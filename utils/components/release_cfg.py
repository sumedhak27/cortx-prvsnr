#!/usr/bin/python3
import json
import sys
import yaml

from argparse import ArgumentParser

from .base_cfg import BaseCfg


class ReleaseCfg(BaseCfg):
    __options = {}
    __cfg_path = ""


    def __init__(self, arg_parser, cfg_path):
        if not arg_parser:
            raise Exception("Class cannot be initialized without an argparse object")

        self.__cfg_path = cfg_path
        self.__setup_args(arg_parser)
        self.__load_defaults()


    def __setup_args(self, arg_parser):
        # TODO - validate for accidental override
        arg_parser.add_argument(
            '--release',
            help='Release version as required in CI repo, e.g. EES_Sprint1')

        arg_parser.add_argument(
            '--release-file',
            dest = 'release_file',
            action="store",
            help='Yaml file with release configs')

        arg_parser.add_argument(
            '--show-release-file-format',
            dest = 'show_release_file_format',
            action="store_true",
            help='Display Yaml file format for release configs')


    def __load_defaults(self):
        with open(self.__cfg_path, 'r') as fd:
            self.__options = yaml.load(fd, Loader=yaml.FullLoader)
        # print(json.dumps(self.__options, indent = 4))
        # TODO validations for configs.


    def process_inputs(self, arg_parser):
        program_args = arg_parser.parse_args()

        if program_args.show_release_file_format:
            print(yaml.dump(self.__options, default_flow_style=False, width=1, indent=4))
            return False

        elif program_args.release_file:
            # Load release file and merge options.
            new_options = {}
            with open(program_args.release_file, 'r') as fd:
                new_options = yaml.load(fd, Loader=yaml.FullLoader)
                self.__options.update(new_options)
            return True

        elif program_args.interactive:
            input_msg = ("Enter target eos release version ({0}): ".format(
                    self.__options["eos_release"]["target_build"]
                )
            )
            self.__options["eos_release"]["target_build"] = (
                input(input_msg)
                or
                self.__options["eos_release"]["target_build"]
            )
            return True

        elif program_args.release:
            self.__options["eos_release"]["target_build"] = program_args.release
            # print(json.dumps(self.__options, indent = 4))
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
