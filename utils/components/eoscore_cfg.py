#!/usr/bin/env python3
import json
import yaml

from argparse import ArgumentParser

from .base_cfg import BaseCfg


class EOSCoreCfg(BaseCfg):
    __options = {}
    __cfg_path = ""


    def __init__(self, arg_parser, cfg_path):
        if not arg_parser:
            raise Exception("Class cannot be initialized without an argparse object")

        self.__cfg_path = cfg_path
        self.__setup_args(arg_parser)
        self.__load_defaults()


    def __setup_args(self, arg_parser) -> bool:
        # TODO - validate for accidental override
        arg_parser.add_argument(
            '--eoscore-file',
            dest = 'eoscore_file',
            action="store",
            help='Yaml file with eoscore configs')

        arg_parser.add_argument(
            '--show-eoscore-file-format',
            dest = 'show_eoscore_file_format',
            action="store_true",
            help='Display Yaml file format for eoscore configs')


    def __load_defaults(self):
        with open(self.__cfg_path, 'r') as fd:
            self.__options = yaml.load(fd, Loader=yaml.FullLoader)
            # print(json.dumps(self._release_options, indent = 4))
            # TODO validations for configs.


    def process_inputs(self, arg_parser: ArgumentParser) -> bool:
        program_args = arg_parser.parse_args()

        if program_args.show_eoscore_file_format:
            print(yaml.dump(self.__options, default_flow_style=False, width=1, indent=4))
            return False

        elif program_args.eoscore_file:
            # Load eoscore file and merge options.
            new_options = {}
            with open(program_args.eoscore_file, 'r') as fd:
                new_options = yaml.load(fd, Loader=yaml.FullLoader)
                self.__options.update(new_options)
            return True

        elif program_args.interactive:
            input_msg = ("Enter Maximum RPC message size to be used for eoscore daemon: ({0}):".format(
                    self.__options["eoscore"]["MERO_M0D_MAX_RPC_MSG_SIZE"]
                )
            )
            self.__options["eoscore"]["MERO_M0D_MAX_RPC_MSG_SIZE"] = (
                input(input_msg)
                or
                self.__options["eoscore"]["MERO_M0D_MAX_RPC_MSG_SIZE"]
            )

            input_msg = ("Enter Maximum BE log size to be used for eoscore daemon: ({0}):".format(
                    self.__options["eoscore"]["MERO_M0D_BELOG_SIZE"]
                )
            )
            self.__options["eoscore"]["MERO_M0D_BELOG_SIZE"] = (
                input(input_msg)
                or
                self.__options["eoscore"]["MERO_M0D_BELOG_SIZE"]
            )

            input_msg = ("Enter Maximum BE segment size to be used for eoscore daemon: ({0}):".format(
                    self.__options["eoscore"]["MERO_M0D_IOS_BESEG_SIZE"]
                )
            )
            self.__options["eoscore"]["MERO_M0D_IOS_BESEG_SIZE"] = (
                input(input_msg)
                or
                self.__options["eoscore"]["MERO_M0D_IOS_BESEG_SIZE"]
            )

            # print(json.dumps(self._options, indent = 4))
            return True

        else:
            # print("WARNING: No usable inputs provided.")
            return False


    def save(self):
        with open(self.__cfg_path, 'w') as fd:
            yaml.dump(self.__options, fd, default_flow_style=False, indent=4)


    def load(self, yaml_file):
        pass


    def validate(self, yaml_string) -> bool:
        pass
