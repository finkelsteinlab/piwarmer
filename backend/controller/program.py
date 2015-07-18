import copy
import json
import logging
import time

log = logging.getLogger(__name__)


def calculate_seconds_left(data):
    assert data.program is not None
    assert data.current_time is not None
    assert data.start_time is not None
    seconds_left = int(max(data.program.total_duration - data.seconds_elapsed, 0))
    return seconds_left


def get_desired_temperature(data):
    assert data.program is not None
    assert data.current_time is not None
    assert data.start_time is not None
    elapsed = data.seconds_elapsed
    for (start, stop), setting in sorted(data.program.settings.items()):
        if stop is None or start <= elapsed < stop:
            return float(setting.get_temperature(elapsed - start))


def get_next_n_settings(n, data):
    # WELCOME TO THE SPAGHETTI FACTORY
    assert n > 0
    total = n
    next_steps = {}
    times_until = {}
    found = False
    elapsed = data.seconds_elapsed
    for (start, stop), setting in sorted(data.program.settings.items()):
        if n == 0:
            break
        if start <= elapsed < stop or found is True:
            time_until = "Now Running" if not found else convert_seconds_to_hhmmss(start - elapsed)
            found = True
            next_steps[total - n] = setting.message
            times_until[total - n] = time_until
            n -= 1
    return next_steps, times_until


def convert_seconds_to_hhmmss(seconds):
    return time.strftime('%H:%M:%S', time.gmtime(seconds))


class TemperatureSetting(object):
    def __init__(self, start_temp, final_temp, duration):
        assert duration is None or duration > 0.0
        self._start_temp = float(start_temp)
        self._final_temp = float(final_temp)
        self._duration = duration
        self._display_message = self._set_display_message()

    def _set_display_message(self):
        if self._duration is None:
            # Hold
            message = "Hold at %s&deg;C" % self._start_temp
        elif abs(self._start_temp - self._final_temp) < 0.001:
            # Set
            message = "%s&deg;C for %s" % (self._start_temp, convert_seconds_to_hhmmss(self._duration))
        else:
            # Linear Gradient
            message = "From %s&deg;C to %s&deg;C over %s" % (self._start_temp, self._final_temp, convert_seconds_to_hhmmss(self._duration))
        return message

    @property
    def message(self):
        return self._display_message

    def get_temperature(self, seconds_into_setting):
        if self._duration is None:
            # Hold setting
            return self._start_temp
        # Set or Linear Gradient
        percentage_done = float(seconds_into_setting) / self._duration
        offset = (self._final_temp - self._start_temp) * percentage_done
        return self._start_temp + offset


class TemperatureProgram(object):
    def __init__(self, json_program):
        self._settings = {}
        self._has_hold = False
        self._total_duration = 0.0
        self._load_json(json_program)

    @property
    def settings(self):
        return self._settings

    @property
    def total_duration(self):
        return self._total_duration

    def _load_json(self, json_program):
        """
        json_program will be a dict like:
        {
          "1": {"mode": "set", "temperature": 80.0, "duration": 300},
          "2": {"mode": "linear", "start_temperature": 80.0, "end_temperature": 37.0, "duration": 3600},
          "3": {"mode": "hold", "temperature": 37.0}
        }

        Modes and attributes supported:

        set: temperature, duration
        repeat: num_repeats
        hold: temperature
        linear: temperature, duration

        :param json_program:    temperature settings for an experiment
        :type json_program:     str

        """
        action = {"set": self._set_temperature,
                  "linear": self._linear,
                  "repeat": self._repeat,
                  "hold": self._hold
                  }
        raw_program = json.loads(json_program)
        for index, parameters in sorted(raw_program.items(), key=lambda x: int(x[0])):
            # Get the mode and remove it from the parameters
            mode = parameters.pop("mode", None)
            # Run the desired action using the parameters given
            # Parameters of methods must match the keys exactly!
            if not self._has_hold:
                action[mode](**parameters)

    def _set_temperature(self, temperature=25.0, duration=60):
        temperature = float(temperature)
        duration = int(duration)
        setting = TemperatureSetting(temperature, temperature, duration)
        self._settings[(self._total_duration, self._total_duration + duration)] = setting
        self._total_duration += duration
        return self

    def _linear(self, start_temperature=60.0, end_temperature=37.0, duration=3600):
        duration = int(duration)
        setting = TemperatureSetting(float(start_temperature), float(end_temperature), duration)
        self._settings[(self._total_duration, self._total_duration + duration)] = setting
        self._total_duration += duration
        return self

    def _repeat(self, num_repeats=3):
        new_settings = []
        for i in range(num_repeats):
            for setting in self._settings:
                new_setting = copy.copy(setting)
                new_settings.append(new_setting)
                self._total_duration += new_setting.duration
        self._settings += new_settings
        return self

    def _hold(self, temperature=25.0):
        setting = TemperatureSetting(float(temperature), float(temperature), None)
        self._settings[(self._total_duration, None)] = setting
        self._has_hold = True
        return self