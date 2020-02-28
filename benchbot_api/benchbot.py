from __future__ import print_function

from enum import Enum, unique
import importlib
import os
import requests
import sys
import time

from .agent import Agent

DEFAULT_ADDRESS = 'benchbot_supervisor'
DEFAULT_PORT = 10000

RESULT_LOCATION = '/tmp/benchbot_result'


class _UnexpectedResponseError(requests.RequestException):

    def __init__(self, http_status_code, *args, **kwargs):
        super(_UnexpectedResponseError, self).__init__(
            "Received an unexpected response from BenchBot supervisor "
            "(HTTP status code: %d)" % http_status_code, *args, **kwargs)


@unique
class ActionResult(Enum):
    SUCCESS = 0,
    FINISHED = 1,
    COLLISION = 2


class BenchBot(object):

    @unique
    class RouteType(Enum):
        CONNECTION = 0,
        CONFIG = 1,
        SIMULATOR = 2,
        STATUS = 3,
        EXPLICIT = 4

    def __init__(self,
                 agent,
                 supervisor_address='http://' + DEFAULT_ADDRESS + ':' +
                 str(DEFAULT_PORT) + '/',
                 auto_start=True):
        if not isinstance(agent, Agent):
            raise ValueError("BenchBot received an agent of type '%s' "
                             "which is not an instance of '%s'." %
                             (agent.__class__.__name__, Agent.__name__))
        self.agent = agent

        self.supervisor_address = supervisor_address
        self._connection_callbacks = {}
        if auto_start:
            self.start()

    def _build_address(self, route_name, route_type=RouteType.CONNECTION):
        base = self.supervisor_address + (
            '' if self.supervisor_address.endswith('/') else '/')
        if route_type == BenchBot.RouteType.CONNECTION:
            return base + 'connections/' + route_name
        elif route_type == BenchBot.RouteType.CONFIG:
            return base + 'config/' + route_name
        elif route_type == BenchBot.RouteType.SIMULATOR:
            return base + 'simulator/' + route_name
        elif route_type == BenchBot.RouteType.STATUS:
            return base + 'status/' + route_name
        elif route_type == BenchBot.RouteType.EXPLICIT:
            return base + route_name
        else:
            raise ValueError(
                "Cannot build address from invalid route type: %s" %
                route_type)

    def _receive(self, route_name=None, route_type=RouteType.CONNECTION):
        try:
            resp = requests.get(self._build_address(route_name, route_type))
            if resp.status_code >= 300:
                raise _UnexpectedResponseError(resp.status_code)
            return resp.json()
        except:
            raise requests.ConnectionError(
                "Failed to establish a connection to BenchBot supervisor")

    def _send(self,
              route_name=None,
              data=None,
              route_type=RouteType.CONNECTION):
        data = {} if data is None else data
        try:
            resp = requests.get(self._build_address(route_name, route_type),
                                json=data)
            if resp.status_code >= 300:
                raise _UnexpectedResponseError(resp.status_code)
        except:
            raise requests.ConnectionError(
                "Failed to establish a connection to BenchBot supervisor with "
                "input data: %s, %s, %s" % (route_name, route_type.name, data))

    @staticmethod
    def _attempt_connection_imports(connection_data):
        if 'callback_api' in connection_data:
            x = connection_data['callback_api'].rsplit('.', 1)
            return getattr(importlib.import_module('benchbot_api.' + x[0]),
                           x[1])
        return None

    @property
    def actions(self):
        return ([] if self._receive(
            'is_collided', BenchBot.RouteType.SIMULATOR)['is_collided'] or
                self._receive('is_finished',
                              BenchBot.RouteType.STATUS)['is_finished'] else
                self._receive('actions', BenchBot.RouteType.CONFIG))

    @property
    def observations(self):
        return self._receive('observations', BenchBot.RouteType.CONFIG)

    @property
    def task_details(self):
        return {
            k: v for k, v in zip(['type', 'control_mode', 'localisation_mode'],
                                 self._receive('task_name', BenchBot.RouteType.
                                               CONFIG).split(':'))
        }

    @property
    def result_filename(self):
        if not os.path.exists(os.path.dirname(RESULT_LOCATION)):
            os.makedirs(os.path.dirname(RESULT_LOCATION))
        return os.path.join(RESULT_LOCATION)

    def reset(self):
        # Only restart the supervisor if it is in a dirty state
        if self._receive('is_dirty', BenchBot.RouteType.SIMULATOR)['is_dirty']:
            print("Dirty simulator state detected. Performing reset ... ",
                  end="")
            sys.stdout.flush()
            self._receive('restart', BenchBot.RouteType.SIMULATOR
                         )  # This should probably be a send...
            print("Complete.")
        return self.step(None)

    def run(self):
        observations, action_result = self.reset()
        while not self.agent.is_done(action_result):
            action, action_args = self.agent.pick_action(
                observations, self.actions)
            observations, action_result = self.step(action, **action_args)
        self.agent.save_result(self.result_filename)

    def start(self):
        # Establish a connection to the supervisor (throw an error on failure)
        print("Waiting to establish connection to a running supervisor ... ",
              end='')
        sys.stdout.flush()
        try:
            self._receive("/", BenchBot.RouteType.EXPLICIT)
        except requests.ConnectionError as e:
            raise type(e)("Could not find a BenchBot supervisor @ '%s'. "
                          "Are you sure it is available?" %
                          self.supervisor_address)
        print("Connected!")

        # Wait until the simulator is running
        print("Waiting to establish connection to a running simulator ... ",
              end='')
        sys.stdout.flush()
        while (not self._receive("is_running",
                                 BenchBot.RouteType.SIMULATOR)['is_running']):
            time.sleep(0.1)
        print("Connected!")

        # Establish connection (throw an error if we can't find the supervisor)

        # Get references to all of the API callbacks in robot config
        self._connection_callbacks = {
            k:
            BenchBot._attempt_connection_imports(v) for k, v in self._receive(
                'robot', BenchBot.RouteType.CONFIG).items()
        }

    def step(self, action, **action_kwargs):
        # Dictionary of mappings between action (str), and args (list of str)
        VALID_ACTIONS = { 'move_next' : [],
                          'move_distance' : ['distance'],
                          'move_angle' : ['angle']
                        }

        # First check if action is a valid action
        if action not in VALID_ACTIONS:
            raise RuntimeError(action + ' not a valid action. Valid actions are "' + ', '.join([k for k in VALID_ACTIONS.keys()]))

        # Then check if we got the right number of arguments
        _action_kwargs_len = len(action_kwargs)
        _valid_action_kwargs_len = len(VALID_ACTIONS[action])
        if _action_kwargs_len != _valid_action_kwargs_len:
            raise RuntimeError(action + ' requires ' + _valid_action_kwargs_len + ' arguments. You supplied ' + _action_kwargs_len)

        # And finally check if the arguments are correct
        for arg_name, _ in action_kwargs.items():
            _invalid_arguments = []
            _valid_arguments = VALID_ACTIONS[action]
            if arg_name not in _valid_arguments:
                _invalid_arguments.append(arg_name)
            if len(_invalid_arguments):
                raise RuntimeError('Valid arguments to ' + action + ' are: ' + ' ,'.join(_valid_arguments) + '. The following arguments are invalid: ' + ' ,'.join(_invalid_arguments))

        # Perform the requested action
        if action is not None:
            print("Sending action '%s' with args: %s" %
                  (action, action_kwargs))
            self._send(action, action_kwargs, BenchBot.RouteType.CONNECTION)

        # Derive action_result (TODO should probably not be this flimsy...)
        action_result = ActionResult.SUCCESS
        if self._receive('is_collided',
                         BenchBot.RouteType.SIMULATOR)['is_collided']:
            action_result = ActionResult.COLLISION
        elif self._receive('is_finished',
                           BenchBot.RouteType.STATUS)['is_finished']:
            action_result = ActionResult.FINISHED

        # Retrieve and return an updated set of observations
        raw_os = {o: self._receive(o) for o in self.observations}
        return ({
            k: self._connection_callbacks[k](v)
            if self._connection_callbacks[k] is not None else v
            for k, v in raw_os.items()
        }, action_result)
