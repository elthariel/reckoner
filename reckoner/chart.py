# -- coding: utf-8 --

# Copyright 2017 Reactive Ops Inc.
#
# Licensed under the Apache License, Version 2.0 (the “License”);
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an “AS IS” BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import subprocess
import os

from collections import OrderedDict
from string import Template

from exception import ReckonerCommandException
from config import Config
from repository import Repository
from helm import Helm
from . import call

default_repository = {'name': 'stable', 'url': 'https://kubernetes-charts.storage.googleapis.com'}


class Chart(object):
    """
    Description:
    - Chart class for each release in the course.yml

    Arguments:
    - chart (dict):

    Attributes:
    - config: Instance of Config()
    - helm: Instance of Helm()
    - release_name : String. Name of the release
    - name: String. Name of chart
    - files: List. Values files
    - namespace

    Returns:
    - Instance of Response() is truthy where Reponse.exitcode == 0
    - Instance of Response() is falsey where Reponse.exitcode != 0
    """

    def __init__(self, chart):
        self.helm = Helm()
        self.config = Config()
        self._release_name = chart.keys()[0]
        self._chart = chart[self._release_name]
        self._repository = Repository(self._chart.get('repository', default_repository))
        self._chart['values'] = self.ordereddict_to_dict(self._chart.get('values', {}))
        value_strings = self._chart.get('values-strings', {})
        self._chart['values_strings'] = self.ordereddict_to_dict(value_strings)
        if value_strings != {}:
            del(self._chart['values-strings'])

    @property
    def release_name(self):
        """
        Returns release name of course chart
        """
        return self._release_name

    @property
    def name(self):
        """
        Retturns chart name of course chart
        """
        return self._chart.get('chart', self._release_name)

    def ordereddict_to_dict(self, value):
        """
        Converts an OrderedDict to a standard dict
        """
        for k, v in value.items():
            if type(v) == OrderedDict:
                value[k] = self.ordereddict_to_dict(v)
            if type(v) == list:
                for item in v:
                    if type(item) == OrderedDict:
                        v.remove(item)
                        v.append(self.ordereddict_to_dict(item))
        return dict(value)

    @property
    def files(self):
        """ List of values files from the course chart """
        return dict(self._chart.get('files', []))

    @property
    def namespace(self):
        """ Namespace to install the course chart """
        return self._namespace

    def __check_env_vars(self, args):
        """
        accepts list of args
        if any of those appear to be env vars
        and are missing from the environment
        an exception is raised
        """
        try:
            [Template(arg).substitute(os.environ) for arg in args]
        except KeyError, e:
            raise Exception("Missing requirement environment variable: {}".format(e.args[0]))

    @property
    def values(self):
        return self._chart.get('values', {})

    @property
    def values_strings(self):
        return self._chart.get('values-string', {})

    @property
    def repository(self):
        """ Repository object parsed from course chart """
        return self._repository

    def __getattr__(self, key):
        return self._chart.get(key)

    def __str__(self):
        return str(dict(self._chart))

    def __get_hook(self, hook_type):
        if self.hooks is not None:
            return self.hooks.get(hook_type)

    def pre_install_hook(self):
        self.run_hook('pre_install')

    def post_install_hook(self):
        self.run_hook('post_install')

    def run_hook(self, hook_type):
        """ Hook Type. Runs the commands defined by the hook """
        coms = self.__get_hook(hook_type)
        if coms is None:
            return coms
        logging.info("Running {} hook.".format(hook_type))
        if type(coms) == str:
            coms = [coms]

        for com in coms:
            if self.config.local_development or self.config.dryrun:
                logging.debug("Hook not run: {}".format(com))
                continue

            logging.debug("Hook: {}".format(com))
            try:
                call(com.split())
            except ReckonerCommandException, e:
                logging.error("{} hook failed to run".format(hook_type))
                logging.error(e.stderr)
                raise e

    def rollback(self):
        """ Rollsback most recent release of the course chart """

        release = [release for release in self.helm.releases.deployed if release.name == self._release_name][0]
        if release:
            release.rollback()

    def update_dependencies(self):
        """ Update the course chart dependencies """
        if self.config.local_development or self.config.dryrun:
            return True
        logging.debug("Updating chart dependencies: {}".format(self.chart_path))
        if os.path.exists(self.chart_path):
            try:
                r = self.helm.dependency_update(self.chart_path)
            except ReckonerCommandException, e:
                logging.warn("Unable to update chart dependancies: {}".format(e.stderr))

    def install(self, namespace):
        """
        Description:
        - Uprade --install the course chart

        Arguments:
        - namespace (string). Passed in but will be overriddne by Chart().namespace if set

        Returns:
        - Bool
        """
        helm = Helm()

        # Set the namespace
        _namespace = self.namespace or namespace
        self.pre_install_hook()
        self.repository.install(self.name, self.version)
        self.chart_path = self.repository.chart_path
        # Update the helm dependencies

        self.update_dependencies()

        # Build the args for the chart installation
        # And add any extra arguments

        args = ['{}'.format(self._release_name), self.chart_path, ]
        args.append('--namespace={}'.format(_namespace))
        args.extend(self.debug_args)
        args.extend(self.helm_args)
        if self.version:
            args.append('--version={}'.format(self.version))
        for file in self.files:
            args.append("-f={}".format(file))

        for key, value in self.values.iteritems():
            for k, v in self._format_set(key, value):
                args.append("--set={}={}".format(k, v))
        for key, value in self.values_strings.iteritems():
            for k, v in self._format_set(key, value):
                args.append("--set-string={}={}".format(k, v))

        self.__check_env_vars(args)
        helm.upgrade(args)
        self.post_install_hook()

    @property
    def debug_args(self):
        """ Returns list of Helm debug arguments """
        if self.config.dryrun:
            return ['--dry-run', '--debug']
        if self.config.debug:
            return ['--debug']

        return []

    @property
    def helm_args(self):
        """ Returns list of extra options/args for the helm command """
        if self.config.helm_args is not None:
            return self.config.helm_args
        return []

    def _format_set(self, key, value):
        """
        Allows nested yaml to be set on the command line of helm.
        Accepts key and value, if value is an ordered dict, recursively
        formats the string properly
        """
        if type(value) == dict:
            for new_key, new_value in value.iteritems():
                for k, v in self._format_set("{}.{}".format(key, new_key), new_value):
                    for a, b in self._format_set_list(k, v):
                        yield a, b
        else:
            for a, b in self._format_set_list(key, value):
                yield a, b

    def _format_set_list(self, key, value):
        """
        given a list and a key, format it properly
        for the helm set list indexing
        """
        if type(value) == list:
            for index, item in enumerate(value):
                if type(item) == dict:
                    for k, v in self._format_set("{}[{}]".format(key, index), item):
                        yield k, v
                else:
                    yield "{}[{}]".format(key, index), item
        else:
            yield key, value
