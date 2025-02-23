#
# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#

import logging
from copy import deepcopy
import ipaddress
import functools
from typing import List, Union, Any, Iterable, Tuple, Dict, Type, Optional
from pathlib import Path
import argparse
import importlib
from enum import Enum

from . import config, utils
from .vendor import attr
from .errors import UnknownParamError, SWUpdateRepoSourceError
from .pillar import (
    KeyPath, PillarKeyAPI, PillarKey, PillarItemsAPI
)
from .param import Param, ParamDictItem
from .api_spec import param_spec
from .values import (
    UNDEFINED, UNCHANGED, NONE, value_from_str,
    is_special
)
from .serialize import PrvsnrType, loads


METADATA_PARAM_GROUP_KEY = '_param_group_key'
METADATA_ARGPARSER = '_param_argparser'

logger = logging.getLogger(__name__)


def load_cli_spec():
    res = utils.load_yaml(config.CLI_SPEC_PATH)

    def _choices_filter(leaf: utils.DictLeaf):
        return (
            leaf.key == 'choices'
            and isinstance(leaf.value, str)
            and leaf.value.startswith(config.CLI_SPEC_PY_OBJS_PREFIX)
        )

    # convert choices to objects
    for leaf in utils.iterate_dict(res, filter_f=_choices_filter):
        choices_spec = leaf.value.split(
            config.CLI_SPEC_PY_OBJS_PREFIX
        )[1].split('.')

        mod_name = '.'.join(choices_spec[0:-1])
        attr_name = choices_spec[-1]
        module = importlib.import_module(mod_name)

        choices = getattr(module, attr_name)

        try:
            if issubclass(choices, Enum):
                choices = [i.value for i in choices]
        except TypeError:
            pass  # not a class

        leaf.parent[leaf.key] = choices

    def _nohelp_filter(leaf: utils.DictLeaf):
        return (
            leaf.key != 'help'
            and ('help' not in leaf.parent)
            and isinstance(leaf.value, str)
        )

    # convert trivial descriptions (no help)
    for leaf in utils.iterate_dict(res, filter_f=_nohelp_filter):
        leaf.parent[leaf.key] = dict(help=leaf.value)

    return res


cli_spec = load_cli_spec()


# TODO IMPROVE use some attr api to copy spec
def copy_attr(_attr, name=None, **changes):
    attr_kw = {}
    for arg in (
        'default', 'validator', 'repr', 'hash',
        'init', 'metadata', 'type', 'converter', 'kw_only'
    ):
        attr_kw[arg] = (
            changes[arg] if arg in changes else getattr(_attr, arg)
        )

    if not name:
        name = _attr.name

    _utility_type = attr.make_class(
        "_UtilityType", {
            name: attr.ib(**attr_kw)
        }
    )

    return attr.fields_dict(_utility_type)[name]


@attr.s(auto_attribs=True)
class AttrParserArgs:
    _attr: Any  # TODO typing
    prefix: str = attr.ib(default=None)

    name: str = attr.ib(init=False, default=None)
    action: str = attr.ib(init=False, default='store')
    metavar: str = attr.ib(init=False, default=None)
    dest: str = attr.ib(init=False, default=None)
    default: str = attr.ib(init=False, default=None)
    const: str = attr.ib(init=False, default=None)
    choices: List = attr.ib(init=False, default=None)
    help: str = attr.ib(init=False, default='')
    type: Any = attr.ib(init=False, default=None)  # TODO typing
    # TODO TEST EOS-8473
    nargs: str = attr.ib(init=False, default=None)

    def __attrs_post_init__(self):  # noqa: C901 FIXME
        self.name = self._attr.name

        if self.name.startswith('__'):
            raise ValueError(
                f"{self.name}: multiple leading underscores are not expected"
            )

        self.name = self.name.lstrip('_')

        if self.prefix:
            self.name = f"{self.prefix}{self.name}"

        parser_args = {}

        parser_args = self._attr.metadata.get(
            METADATA_ARGPARSER, {}
        )

        if parser_args.get('choices'):
            self.choices = parser_args.get('choices')

        if parser_args.get('action'):
            self.action = parser_args.get('action')
        elif self._attr.type is bool:
            self.action = 'store_true'

        self.type = parser_args.get(
            'type',
            functools.partial(
                self.value_from_str, v_type=self._attr.type
            )
        )

        for arg in ('help', 'dest', 'const'):
            if arg in parser_args:
                setattr(self, arg, parser_args.get(arg))

        if self.choices:
            self.help = (
                '{} [choices: {}]'
                .format(self.help, ', '.join(self.choices))
            )

        # TODO TEST EOS-8473
        if parser_args.get('nargs'):
            self.nargs = parser_args.get('nargs')

        if self._attr.default is not attr.NOTHING:
            # optional argument
            self.name = '--' + self.name.replace('_', '-')
            # default value for an object (attr) might be more
            # complicated than for a parser
            default_v = parser_args.get('default', self._attr.default)
            if isinstance(default_v, attr.Factory):
                default_v = default_v.factory()
            self.default = default_v
            self.metavar = parser_args.get('metavar')
            if not self.metavar and self._attr.type:
                self.metavar = getattr(self._attr.type, '__name__', None)
            if self.metavar:
                self.metavar = self.metavar.upper()

    @property
    def kwargs(self):
        def _filter(_attr, value):
            not_filter = ['_attr', 'name', 'prefix']
            if self.action in ('store_true', 'store_false'):
                not_filter.extend(['metavar', 'type', 'default'])
            if self.action in ('store_const',):
                not_filter.extend(['type'])
            # TODO TEST EOS-8473 nargs
            for arg in ('choices', 'dest', 'const', 'nargs'):
                if getattr(self, arg) is None:
                    not_filter.append(arg)
            return _attr.name not in not_filter

        return attr.asdict(self, filter=_filter)

    @classmethod
    def value_from_str(cls, value, v_type=None):
        _value = value_from_str(value)
        if _value is NONE:
            _value = None
        elif isinstance(_value, str):
            if (v_type is List) or (v_type == 'json'):
                _value = loads(value)
        return _value


class InputAttrParserArgs(AttrParserArgs):
    @classmethod
    def value_from_str(cls, value, v_type=None):
        _value = super().value_from_str(value, v_type=v_type)
        return UNCHANGED if _value is None else _value


class ParserFiller:
    @staticmethod
    def prepare_args(
        cls, attr_parser_cls: Type[AttrParserArgs] = AttrParserArgs
    ):
        res = {}
        for _attr in attr.fields(cls):
            if METADATA_ARGPARSER in _attr.metadata:
                parser_prefix = getattr(cls, 'parser_prefix', None)
                metadata = _attr.metadata[METADATA_ARGPARSER]

                if isinstance(metadata, str):
                    metadata = KeyPath(metadata).value(cli_spec)
                    _attr = copy_attr(
                        _attr, metadata={
                            METADATA_ARGPARSER: metadata
                        }
                    )

                if metadata.get('action') == 'store_bool':
                    for name, default, m_changes in (
                        (_attr.name, _attr.default, {
                            'help': f"enable {metadata['help']}",
                            'action': 'store_const',
                            'const': True,
                            'dest': _attr.name,
                        }), (f'no{_attr.name}', not _attr.default, {
                            'help': f"disable {metadata['help']}",
                            'action': 'store_const',
                            'const': False,
                            'dest': _attr.name,
                        })
                    ):
                        metadata_copy = deepcopy(metadata)
                        metadata_copy.update(m_changes)
                        attr_copy = copy_attr(
                            _attr, name=name, default=default, metadata={
                                METADATA_ARGPARSER: metadata_copy
                            }
                        )
                        args = attr_parser_cls(attr_copy, prefix=parser_prefix)
                        res[args.name] = args.kwargs
                else:
                    args = attr_parser_cls(_attr, prefix=parser_prefix)
                    res[args.name] = args.kwargs

        return res

    @staticmethod
    def fill_parser(cls, parser, attr_parser_cls=AttrParserArgs):
        _args = ParserFiller.prepare_args(cls, attr_parser_cls)
        for name, kwargs in _args.items():
            parser.add_argument(name, **kwargs)

    @staticmethod
    def extract_args(
        cls, kwargs, positional=True, optional=True, pop=True
    ):
        _args = {}
        _kwargs = {}

        parser_prefix = getattr(cls, 'parser_prefix', '')

        for _attr in attr.fields(cls):
            if METADATA_ARGPARSER in _attr.metadata:
                # name = (
                #     _attr.name.split(parser_prefix, 1)[-1]
                #     if parser_prefix else _attr.name
                # )
                arg_name = f"{parser_prefix}{_attr.name}".replace('-', '_')
                if arg_name in kwargs:
                    _dest = None
                    if positional and _attr.default is attr.NOTHING:
                        _dest = _args
                    elif optional and _attr.default is not attr.NOTHING:
                        _dest = _kwargs

                    if _dest is not None:
                        _dest[_attr.name] = kwargs[arg_name]
                        if pop:
                            kwargs.pop(arg_name)

        return _args.values(), _kwargs, kwargs

    @staticmethod
    def extract_positional_args(cls, kwargs):
        _args, _, kwargs = ParserFiller.extract_args(
            cls, kwargs, positional=True, optional=False
        )
        return _args, kwargs

    @staticmethod
    def extract_optional_args(cls, parsed_args):
        _, _kwargs, kwargs = ParserFiller.extract_args(
            cls, parsed_args, positional=False, optional=True
        )
        return _kwargs, kwargs

    @staticmethod
    def from_args(cls, parsed_args: Union[dict, argparse.Namespace], pop=True):
        if isinstance(parsed_args, argparse.Namespace):
            _parsed_args = vars(parsed_args)

        _args, _kwargs, _parsed_args = ParserFiller.extract_args(
            cls, _parsed_args, positional=True, optional=True, pop=pop
        )

        if pop:
            parsed_args = _parsed_args

        return cls(*_args, **_kwargs), parsed_args


@attr.s(auto_attribs=True)
class ParserMixin:

    parser_prefix = ''

    @classmethod
    def parser_attrs(cls):
        for _attr in attr.fields(cls):
            if METADATA_ARGPARSER in _attr.metadata:
                yield _attr

    @classmethod
    def parser_args(cls):
        for _attr in cls.parser_attrs():
            yield f"{cls.parser_prefix}{_attr.name.replace('_', '-')}"

    @classmethod
    def prepare_args(cls, *args, **kwargs):
        return ParserFiller.prepare_args(cls, *args, **kwargs)

    @classmethod
    def fill_parser(cls, parser, *args, **kwargs):
        return ParserFiller.fill_parser(cls, parser, *args, **kwargs)

    @classmethod
    def from_args(cls, parsed_args, *args, **kwargs):
        return ParserFiller.from_args(cls, parsed_args, *args, **kwargs)[0]


@attr.s(auto_attribs=True)
class NoParams:
    @classmethod
    def fill_parser(cls, parser):
        pass

    @classmethod
    def extract_positional_args(cls, kwargs):
        return (), kwargs


@attr.s(auto_attribs=True, frozen=True)
class PillarKeysList:
    _keys: List[PillarKey] = attr.Factory(list)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    @classmethod
    def from_args(
        cls,
        *args: Tuple[Union[str, Tuple[str, str]], ...]
    ):
        pi_keys = []
        for arg in args:
            if type(arg) is str:
                pi_keys.append(PillarKey(arg))
            elif type(arg) is tuple:
                # TODO IMPROVE more checks for tuple types and len
                pi_keys.append(PillarKey(*arg))
            else:
                raise TypeError(f"Unexpected type {type(arg)} of args {arg}")
        return cls(pi_keys)

    @classmethod
    def fill_parser(cls, parser):
        parser.add_argument(
            'args', metavar='keypath', type=str, nargs='*',
            help='a pillar key path'
        )

    @classmethod
    def extract_positional_args(cls, kwargs):
        return (), kwargs


@attr.s(auto_attribs=True, frozen=True)
class PillarInputBase(PillarItemsAPI):
    keypath: str = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': 'pillar key path',
                # 'metavar': 'value'
            }
        }
    )
    # TODO IMPROVE use some constant for json type
    value: Any = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': 'pillar value',
                'type': functools.partial(
                    AttrParserArgs.value_from_str, v_type='json'
                )
            }
        }
    )
    fpath: str = attr.ib(
        default=None,
        metadata={
            METADATA_ARGPARSER: {
                'help': (
                    'file path relative to pillar roots, '
                    'if not specified <key-path-top-level-part>.sls is used'
                ),
                # 'metavar': 'value'
            }
        }
    )

    def pillar_items(self) -> Iterable[Tuple[PillarKeyAPI, Any]]:
        return (
            (PillarKey(self.keypath, self.fpath), self.value),
        )

    @classmethod
    def from_args(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    @classmethod
    def fill_parser(cls, parser):
        ParserFiller.fill_parser(cls, parser, AttrParserArgs)

    @classmethod
    def extract_positional_args(cls, kwargs):
        return ParserFiller.extract_positional_args(cls, kwargs)


@attr.s(auto_attribs=True)
class ParamsList:
    params: List[Param]

    def __iter__(self):
        return iter(self.params)

    @classmethod
    def from_args(cls, *args: List[Union[str, Param]]):
        params = []
        for param in args:
            key_path = KeyPath(str(param))
            param = param_spec.get(str(key_path))
            if param is None:
                param_di = param_spec.get(str(key_path.parent))
                if isinstance(param_di, ParamDictItem):
                    param = Param(
                        key_path,
                        (param_di.keypath / key_path.leaf, param_di.fpath)
                    )
                else:
                    logger.error(
                        "Unknown param {}".format(key_path)
                    )
                    raise UnknownParamError(str(key_path))
            params.append(param)
        return cls(params)

    @classmethod
    def fill_parser(cls, parser):
        parser.add_argument(
            'args', metavar='param', type=str, nargs='+',
            help='a param name to get'
        )

    @classmethod
    def extract_positional_args(cls, kwargs):
        return (), kwargs


class ParamGroupInputBase(PillarItemsAPI):
    _param_group = None
    _spec = None

    def pillar_items(self):  # TODO return type
        res = {}
        for attr_name in attr.fields_dict(type(self)):
            res[self.param_spec(attr_name)] = getattr(self, attr_name)
        return iter(res.items())

    @classmethod
    def param_spec(cls, attr_name: str):
        if cls._spec is None:
            cls._spec = {}
        if attr_name not in cls._spec:
            try:
                _attr = attr.fields_dict(cls)[attr_name]
            except KeyError:
                logger.error("unknown attr {}".format(attr_name))
                raise ValueError('unknown attr {}'.format(attr_name))
            else:
                # TODO TEST
                param_group = _attr.metadata.get(METADATA_PARAM_GROUP_KEY, '')
                full_path = (
                    "{}/{}".format(param_group, attr_name) if param_group
                    else attr_name
                )
                cls._spec[attr_name] = param_spec[full_path]
        return cls._spec[attr_name]

    @classmethod
    def from_args(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    @classmethod
    def fill_parser(cls, parser):
        ParserFiller.fill_parser(cls, parser, InputAttrParserArgs)

    @classmethod
    def extract_positional_args(cls, kwargs):
        return ParserFiller.extract_positional_args(cls, kwargs)

    @staticmethod
    def _attr_ib(
        param_group='', default=UNCHANGED, descr='', metavar=None, **kwargs
    ):
        return attr.ib(
            default=default,
            metadata={
                METADATA_PARAM_GROUP_KEY: param_group,
                METADATA_ARGPARSER: {
                    'help': descr,
                    'metavar': metavar
                }
            },
            **kwargs
        )


class Validation():
    @staticmethod
    def check_ip4(instace, attribute, value):
        try:
            ip = None
            if (
                value and
                value != UNCHANGED and
                value != 'None' and
                value != '\"\"'
            ):  # FIXME JBOD
                ip = ipaddress.IPv4Address(value)
                # TODO : Improve logic internally convert ip to
                # canonical forms.
                if str(ip) != value:
                    raise ValueError(
                        "IP is not in canonical form."
                        f"Canonical form of IP can be {str(ip)}"
                    )
        except ValueError as exc:
            raise ValueError(
                f"{attribute.name}: invalid ip4 address {value} "
                f"Error: {str(exc)}"
            )


@attr.s(auto_attribs=True)
class NTP(ParamGroupInputBase):
    _param_group = 'ntp'
    # TODO some trick to avoid passing that value
    server: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="ntp server ip"
    )
    timezone: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="ntp server timezone"
    )


@attr.s(auto_attribs=True)
class Hostname(ParamGroupInputBase):
    _param_group = 'hostname'
    hostname: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="hostname to be set"
    )


@attr.s(auto_attribs=True)
class Firewall(ParamGroupInputBase):
    _param_group = 'firewall'


@attr.s(auto_attribs=True)
class MgmtNetwork(ParamGroupInputBase):
    _param_group = 'mgmt_network'
    mgmt_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node mgmt gateway IP",
        validator=Validation.check_ip4
    )
    mgmt_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management interface IP",
        validator=Validation.check_ip4
    )
    mgmt_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management interface netmask",
        validator=Validation.check_ip4
    )
    mgmt_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management network interfaces"
    )
    mgmt_mtu: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management network mtu",
        default=1500
    )


@attr.s(auto_attribs=True)
class PublicDataNetwork(ParamGroupInputBase):
    _param_group = 'public_data_network'
    data_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node public data interface IP",
        validator=Validation.check_ip4
    )
    data_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data gateway IP",
        validator=Validation.check_ip4
    )
    data_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data interface netmask",
        validator=Validation.check_ip4
    )
    data_public_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node public data network interfaces"
    )
    data_mtu: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data network mtu",
        default=1500
    )


@attr.s(auto_attribs=True)
class PrivateDataNetwork(ParamGroupInputBase):
    _param_group = 'private_data_network'
    data_private_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node private data interface IP",
        validator=Validation.check_ip4
    )
    data_private_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node private data network interfaces"
    )
    data_mtu: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data network mtu",
        default=1500
    )
    data_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data gateway IP",
        validator=Validation.check_ip4
    )
    data_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data interface netmask",
        validator=Validation.check_ip4
    )


class ReleaseParams():
    _param_group = 'release'
    target_build: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Cortx deployment build"
    )


@attr.s(auto_attribs=True)
class Release(ParamGroupInputBase):
    target_build: str = ReleaseParams.target_build


class StorageEnclosureParams():
    _param_group = 'storage'
    enclosure_id: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="Enclosure ID"
    )
    type: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Type of storage"
    )
    primary_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Controller A IP",
        validator=Validation.check_ip4
    )
    secondary_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Controller B IP",
        validator=Validation.check_ip4
    )
    controller_user: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Controller user"
    )
    # TODO IMPROVE EOS-14361 mask secret
    controller_secret: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Controller password"
    )
    controller_type: str = ParamGroupInputBase._attr_ib(
        _param_group, descr=" Controller type"
    )


@attr.s(auto_attribs=True)
class StorageEnclosure(ParamGroupInputBase):
    controller_a_ip: str = StorageEnclosureParams.primary_ip
    controller_b_ip: str = StorageEnclosureParams.secondary_ip
    controller_user: str = StorageEnclosureParams.controller_user
    controller_secret: str = StorageEnclosureParams.controller_secret


class NodeParams():
    _param_group = 'node'
    hostname: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node hostname"
    )
    roles: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="List of roles assigned to the node"
    )
    data_public_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data public network interfaces"
    )
    data_private_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data private network interfaces"
    )
    data_private_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data interface private IP",
        validator=Validation.check_ip4
    )
    data_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data interface IP", default=UNCHANGED,
        validator=Validation.check_ip4
    )
    data_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data interface netmask",
        validator=Validation.check_ip4
    )
    data_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node data gateway IP",
        validator=Validation.check_ip4
    )
    bmc_user: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node BMC User"
    )
    # TODO IMPROVE EOS-14361 mask secret
    bmc_secret: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node BMC password"
    )
    mgmt_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node mgmt gateway IP",
        validator=Validation.check_ip4
    )
    mgmt_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management interface IP",
        validator=Validation.check_ip4
    )
    mgmt_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management interface netmask",
        validator=Validation.check_ip4
    )
    mgmt_interfaces: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node management network interfaces"
    )
    bmc_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="node BMC  IP", default=UNCHANGED,
        validator=Validation.check_ip4
    )
    cvg: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="node storage Cylinder Volume Group (CVG) devices",
        default=UNCHANGED
    )


class NetworkParams():
    _param_group = 'network'
    cluster_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="cluster ip address for public data network",
        validator=Validation.check_ip4
    )
    mgmt_vip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="virtual ip address for management network",
        validator=Validation.check_ip4
    )
    dns_servers: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="list of dns servers as json"
    )
    search_domains: List = ParamGroupInputBase._attr_ib(
        _param_group, descr="list of search domains as json"
    )
    primary_hostname: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node hostname"
    )
    primary_data_roaming_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node data roaming IP"
    )
    primary_data_floating_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node floating IP"
    )
    primary_data_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node data gateway IP"
    )
    primary_mgmt_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node mgmt gateway IP"
    )
    primary_mgmt_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node management interface IP"
    )
    primary_mgmt_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node management interface netmask"
    )
    primary_data_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node data interface IP",
        validator=Validation.check_ip4
    )
    primary_data_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node data interface netmask"
    )
    primary_bmc_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node BMC  IP",
        validator=Validation.check_ip4
    )
    primary_bmc_user: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node BMC User"
    )
    # TODO IMPROVE EOS-14361 mask secret
    primary_bmc_secret: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="primary node BMC password"
    )
    secondary_hostname: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node hostname"
    )
    secondary_data_roaming_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node data roaming IP"
    )
    secondary_data_floating_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node floating IP"
    )
    secondary_mgmt_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node mgmt gateway IP"
    )
    secondary_mgmt_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node management interface IP",
        validator=Validation.check_ip4
    )
    secondary_mgmt_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node management interface netmask"
    )
    secondary_data_gateway: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node data gateway IP"
    )
    secondary_data_public_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node node data interface IP",
        validator=Validation.check_ip4
    )
    secondary_data_netmask: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node data interface netmask"
    )
    secondary_bmc_ip: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node BMC  IP",
        validator=Validation.check_ip4
    )
    secondary_bmc_user: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node BMC User"
    )
    # TODO IMPROVE EOS-14361 mask secret
    secondary_bmc_secret: str = ParamGroupInputBase._attr_ib(
        _param_group, descr="secondary node BMC password"
    )


@attr.s(auto_attribs=True)
class Network(ParamGroupInputBase):
    cluster_ip: str = NetworkParams.cluster_ip
    mgmt_vip: str = NetworkParams.mgmt_vip
    dns_servers: List = NetworkParams.dns_servers
    search_domains: List = NetworkParams.search_domains
    primary_hostname: str = NetworkParams.primary_hostname
    primary_data_roaming_ip: str = NetworkParams.primary_data_roaming_ip
    primary_data_floating_ip: str = NetworkParams.primary_data_floating_ip
    primary_mgmt_public_ip: str = NetworkParams.primary_mgmt_public_ip
    primary_mgmt_netmask: str = NetworkParams.primary_mgmt_netmask
    primary_mgmt_gateway: str = NetworkParams.primary_mgmt_gateway
    primary_data_netmask: str = NetworkParams.primary_data_netmask
    primary_data_gateway: str = NetworkParams.primary_data_gateway
    primary_data_public_ip: str = NetworkParams.primary_data_public_ip
    primary_bmc_ip: str = NetworkParams.primary_bmc_ip
    primary_bmc_user: str = NetworkParams.primary_bmc_user
    primary_bmc_secret: str = NetworkParams.primary_bmc_secret
    secondary_hostname: str = NetworkParams.secondary_hostname
    secondary_data_roaming_ip: str = NetworkParams.secondary_data_roaming_ip
    secondary_data_floating_ip: str = NetworkParams.secondary_data_floating_ip
    secondary_mgmt_public_ip: str = NetworkParams.secondary_mgmt_public_ip
    secondary_mgmt_netmask: str = NetworkParams.secondary_mgmt_netmask
    secondary_data_gateway: str = NetworkParams.secondary_data_gateway
    secondary_mgmt_gateway: str = NetworkParams.secondary_mgmt_gateway
    secondary_data_netmask: str = NetworkParams.secondary_data_netmask
    secondary_bmc_ip: str = NetworkParams.secondary_bmc_ip
    secondary_bmc_user: str = NetworkParams.secondary_bmc_user
    secondary_bmc_secret: str = NetworkParams.secondary_bmc_secret
    secondary_data_public_ip: str = NetworkParams.secondary_data_public_ip


# # TODO TEST
# @attr.s(auto_attribs=True)
# class ClusterIP(ParamGroupInputBase):
#     _param_group = 'network'
#     # dns_server: str = ParamGroupInputBase._attr_ib(_param_group)
#     cluster_ip: str = ParamGroupInputBase._attr_ib(
#         _param_group, descr="cluster ip address for public data network",
#         default=attr.NOTHING
#     )


# # TODO TEST
# @attr.s(auto_attribs=True)
# class MgmtVIP(ParamGroupInputBase):
#     _param_group = 'network'
#     # dns_server: str = ParamGroupInputBase._attr_ib(_param_group)
#     mgmt_vip: str = ParamGroupInputBase._attr_ib(
#         _param_group, descr="virtual ip address for management network",
#         default=attr.NOTHING
#     )


# TODO
# verify that attributes match _param_di during class declaration:
#   - both attributes should satisfy _param_di
#   - is_key might be replaced with checking attr name against _param_di.key
class ParamDictItemInputBase(PrvsnrType, PillarItemsAPI):
    _param_di = None
    _param = None

    def pillar_items(self):  # TODO return type
        return iter({
            self.param_spec(): getattr(self, self._param_di.value)
        }.items())

    def param_spec(self):
        if self._param is None:
            key = getattr(self, self._param_di.key)
            self._param = Param(
                self._param_di.name / key,
                (self._param_di.keypath / key, self._param_di.fpath)
            )
        return self._param

    @classmethod
    def from_args(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    @classmethod
    def fill_parser(cls, parser):
        ParserFiller.fill_parser(cls, parser, InputAttrParserArgs)

    @classmethod
    def extract_positional_args(cls, kwargs):
        return ParserFiller.extract_positional_args(cls, kwargs)

    @staticmethod
    def _attr_ib(
        is_key=False, default=UNCHANGED, descr='', metavar=None, **kwargs
    ):
        return attr.ib(
            default=attr.NOTHING if is_key else default,
            metadata={
                METADATA_ARGPARSER: {
                    'help': descr,
                    'metavar': metavar
                }
            },
            **kwargs
        )


@attr.s(auto_attribs=True)
class SWUpdateRepo(ParamDictItemInputBase):
    _param_di = param_spec['swupdate/repo']
    release: str = ParamDictItemInputBase._attr_ib(
        is_key=True,
        descr="release version"
    )
    source: Union[str, Path] = ParamDictItemInputBase._attr_ib(
        descr=(
            "repo source, might be a local path to a repo folder or iso file"
            " or an url to a remote repo, "
            "{} might be used to remove the repo"
            .format(UNDEFINED)
        ),
        metavar='str',
        converter=lambda v: (
            UNCHANGED if v is None else (
                v if is_special(v) or isinstance(v, Path) else str(v)
            )
        )
    )
    _repo_params: Dict = attr.ib(init=False, default=attr.Factory(dict))
    _metadata: Dict = attr.ib(init=False, default=attr.Factory(dict))

    @source.validator
    def _check_source(self, attribute, value):
        if is_special(value):
            return  # TODO does any special is expected here

        if (
            type(self.source) is str
            and value.startswith(('http://', 'https://'))
        ):
            return

        reason = None
        _value = Path(str(value))
        if _value.exists():
            if _value.is_file():
                if _value.suffix != '.iso':
                    reason = 'not an iso file'
            elif not _value.is_dir():
                reason = 'not a file or directory'
        else:
            reason = 'unexpected type of source'

        if reason:
            logger.error(
                "Invalid source {}: {}"
                .format(str(value), reason)
            )
            raise SWUpdateRepoSourceError(str(value), reason)

    def __attrs_post_init__(self):
        if (
            type(self.source) is str
            and not self.source.startswith(('http://', 'https://'))
        ):
            self.source = Path(self.source)

        if isinstance(self.source, Path):
            self.source = self.source.resolve()

    @property
    def pillar_key(self):
        return self.release

    @property
    def pillar_value(self):
        if self.is_special() or self.is_remote():
            return self.source
        else:
            source = 'iso' if self.source.is_file() else 'dir'
            if self._repo_params:
                return {
                    'source': source,
                    'params': self._repo_params
                }
            else:
                return source

    @property
    def repo_params(self):
        return self._repo_params

    @repo_params.setter
    def repo_params(self, params: Dict):
        self._repo_params = params

    @property
    def metadata(self):
        return self._metadata

    @metadata.setter
    def metadata(self, metadata: Dict):
        self._metadata = metadata

    def is_special(self):
        return is_special(self.source)

    def is_local(self):
        return not self.is_special() and isinstance(self.source, Path)

    def is_remote(self):
        return not (self.is_special() or self.is_local())

    def is_dir(self):
        return self.is_local() and self.source.is_dir()

    def is_iso(self):
        return self.is_local() and self.source.is_file()


@attr.s(auto_attribs=True)
class SWUpgradeRepo(SWUpdateRepo):
    source: Union[str, Path] = ParamDictItemInputBase._attr_ib(
        descr=(
            "repo source, might be a local path to a repo folder or iso file"
            " or an url to a remote repo, "
            "{} might be used to remove the repo"
            .format(UNDEFINED)
        ),
        is_key=True,
        metavar='str',
        converter=lambda v: (
            UNCHANGED if v is None else (
                v if is_special(v) or isinstance(v, Path) else str(v)
            )
        )
    )
    release: str = ParamDictItemInputBase._attr_ib(
        descr="release version",
        default=None
    )
    hash: Optional[Union[str, Path]] = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': ("Path to the file with ISO hash check sum or string"
                         "with format: either '<hash_type>:<hex_hash>' or"
                         "'<hex_hash>'")
            }
        },
        default=None
    )
    hash_type: Optional[str] = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': "Optional: hash type of `hash` parameter",
                'choices': list(map(lambda elem: elem.value, config.HashType))
            }
        },
        validator=attr.validators.optional(
            attr.validators.in_(config.HashType)
        ),
        default=None,
        converter=lambda x: x and config.HashType(str(x))
    )
    sig_file: Optional[Union[str, Path]] = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': "Path to the file with ISO signature"
            }
        },
        converter=utils.converter_path_resolved,
        validator=utils.validator_path_exists,
        default=None
    )
    gpg_pub_key: str = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': "(Optional) Path to the custom GPG public key"
            }
        },
        validator=attr.validators.optional(utils.validator_path_exists),
        converter=utils.converter_path_resolved,
        default=None
    )
    import_pub_key: bool = attr.ib(
        metadata={
            METADATA_ARGPARSER: {
                'help': ("(Optional) Specifies whether to import a given GPG "
                         "public key or not")
            }
        },
        default=False
    )
    # NOTE: source version parameter defines SW Upgrade ISO structure
    source_version: config.ISOVersion = ParamDictItemInputBase._attr_ib(
        descr="SW upgrade source version",
        validator=attr.validators.optional(
            attr.validators.in_(config.ISOVersion)
        ),
        converter=lambda x: config.ISOVersion(str(x)),
        default=config.ISOVersion.VERSION1.value  # Legacy version by default
    )
    _param_di = param_spec['swupgrade/repo']
    # file path to base directory for SW upgrade
    _target_build: str = attr.ib(default=None)
    # parameter to enable or disabled repository
    _enabled: bool = attr.ib(default=False)

    @property
    def _pillar_values_ver1(self):
        # source = 'iso' if self.source.is_file() else 'dir'
        iso_dir = config.PRVSNR_USER_FILES_SWUPGRADE_REPOS_DIR
        return {
            f'{self.release}': {
                'source': f'salt://{iso_dir}/{self.release}.iso',
                'version': f'{self.source_version.value}',
                'is_repo': False
            },
            f'{config.OS_ISO_DIR}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.OS_ISO_DIR}'),
                'is_repo': True,
                # FIXME upgrade iso currently may lack repodata
                # 'enabled': self.enabled
                'enabled': False
            },
            f'{config.CORTX_ISO_DIR}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.CORTX_ISO_DIR}'),
                'is_repo': True,
                'enabled': self.enabled
            },
            f'{config.CORTX_3RD_PARTY_ISO_DIR}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.CORTX_3RD_PARTY_ISO_DIR}'),
                'is_repo': True,
                'enabled': self.enabled
            },
            f'{config.CORTX_PYTHON_ISO_DIR}': {
                'source': f'file://{self.target_build}/{self.release}/'
                          f'{config.CORTX_PYTHON_ISO_DIR}',
                'is_repo': False
            }
        }

    @property
    def _pillar_values_ver2(self):
        """
        Construct the map of pillar values for SW upgrade ISO of version 2
        Returns
        -------

        """
        # source = 'iso' if self.source.is_file() else 'dir'
        iso_dir = config.PRVSNR_USER_FILES_SWUPGRADE_REPOS_DIR
        return {
            f'{self.release}': {
                'source': f'salt://{iso_dir}/{self.release}.iso',
                'version': f'{self.source_version.value}',
                'is_repo': False
            },
            f'{config.ISOKeywordsVer2.FW}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.ISOKeywordsVer2.FW}'),
                'is_repo': False,  # fw is not a repo
                'enabled': self.enabled
            },
            f'{config.ISOKeywordsVer2.OS}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.ISOKeywordsVer2.OS}'),
                'is_repo': False,  # os contains only system patches
                'enabled': self.enabled
            },
            f'{config.UpgradeReposVer2.CORTX.value}': {
                'source': (f'file://{self.target_build}/{self.release}/'
                           f'{config.ISOKeywordsVer2.SW}/'
                           f'{config.UpgradeReposVer2.CORTX.value}'),
                'is_repo': True,
                'enabled': self.enabled
            },
            f'{config.ISOKeywordsVer2.PYTHON}': {
                'source': f'file://{self.target_build}/{self.release}/'
                          f'{config.ISOKeywordsVer2.SW}/'
                          f'{config.ISOKeywordsVer2.EXTERNAL}/'
                          f'{config.ISOKeywordsVer2.PYTHON}',
                'is_repo': False
            },
            f'{config.UpgradeReposVer2.EPEL_7.value}': {
                'source': f'file://{self.target_build}/{self.release}/'
                          f'{config.ISOKeywordsVer2.SW}/'
                          f'{config.ISOKeywordsVer2.EXTERNAL}/'
                          f'{config.ISOKeywordsVer2.RPM}/'
                          f'{config.UpgradeReposVer2.EPEL_7.value}',
                'is_repo': True
            },
            f'{config.UpgradeReposVer2.COMMONS.value}': {
                'source': f'file://{self.target_build}/{self.release}/'
                          f'{config.ISOKeywordsVer2.SW}/'
                          f'{config.ISOKeywordsVer2.EXTERNAL}/'
                          f'{config.ISOKeywordsVer2.RPM}/'
                          f'{config.UpgradeReposVer2.COMMONS.value}',
                'is_repo': True
            },
            f'{config.UpgradeReposVer2.PERFORMANCE.value}': {
                'source': f'file://{self.target_build}/{self.release}/'
                          f'{config.ISOKeywordsVer2.SW}/'
                          f'{config.ISOKeywordsVer2.EXTERNAL}/'
                          f'{config.ISOKeywordsVer2.RPM}/'
                          f'{config.UpgradeReposVer2.PERFORMANCE.value}',
                'is_repo': True
            }
        }

    @property
    def pillar_key(self):
        # the local root pillar key for 'swupgrade' installation type
        return self.release

    @property
    def pillar_value(self):
        if self.is_special():
            res = {
                f"{repo}": None
                for repo in (config.OS_ISO_DIR,
                             config.CORTX_ISO_DIR,
                             config.CORTX_3RD_PARTY_ISO_DIR,
                             config.CORTX_PYTHON_ISO_DIR)
            }
            res[self.release] = None

            return res
        elif self.is_remote():
            # TODO: EOS-20669: Need to save version of remote repo structure,
            #  e.g. self.source_version.
            return {
                f'{config.OS_ISO_DIR}': {
                    'source': f'{self.source}/{config.OS_ISO_DIR}',
                    'is_repo': True,
                    'enabled': self.enabled
                },
                f'{config.CORTX_ISO_DIR}': {
                    'source': f'{self.source}/{config.CORTX_ISO_DIR}',
                    'is_repo': True,
                    'enabled': self.enabled
                },
                f'{config.CORTX_3RD_PARTY_ISO_DIR}': {
                    'source': (f'{self.source}/'
                               f'{config.CORTX_3RD_PARTY_ISO_DIR}'),
                    'is_repo': True,
                    'enabled': self.enabled
                },
                f'{config.CORTX_PYTHON_ISO_DIR}': {
                    'source': f'{self.source}/{config.CORTX_PYTHON_ISO_DIR}',
                    'is_repo': False
                }
            }
        else:
            return (self._pillar_values_ver1
                    if self.source_version == config.ISOVersion.VERSION1
                    else self._pillar_values_ver2)


@attr.s(auto_attribs=True)
class SWUpgradeRemoveRepo(ParamDictItemInputBase):
    _param_di = param_spec['swupgrade/repo']
    release: str = ParamDictItemInputBase._attr_ib(
        is_key=True,
        descr="release version",
        # TODO: It is the rough version of regex because we didn't have the
        #  final representation of the release version from the RE team.
        validator=attr.validators.matches_re(
            r"^[0-9]+\.[0-9]+\.[0-9]+\-[0-9]+$"),
        converter=str
    )

    @property
    def pillar_key(self):
        # the local root pillar key for 'swupgrade' installation type
        return self.release

    @property
    def pillar_value(self):
        res = {
            f"{repo}": None
            for repo in (config.OS_ISO_DIR,
                         config.CORTX_ISO_DIR,
                         config.CORTX_3RD_PARTY_ISO_DIR,
                         config.CORTX_PYTHON_ISO_DIR,
                         self.release)
        }

        return res
