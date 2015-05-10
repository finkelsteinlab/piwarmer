from datetime import datetime
from itertools import cycle
import json
import logging
import redis
import smbus
import time

log = logging.getLogger()
log.addHandler(logging.StreamHandler())
log.setLevel(logging.DEBUG)

try:
    import RPi.GPIO as GPIO
except (SystemError, ImportError):
    log.warn("Not running on Raspberry Pi, GPIO cannot be imported.")


class Data(redis.StrictRedis):
    def update_temperature(self, temp, history_key, timestamp):
        self.set("current_temp", temp)
        self.hset(history_key, timestamp, temp)

    def deactivate(self):
        self.set("active", 0)
        self.delete("program")

    def activate(self):
        self.set("active", 1)

    @property
    def program(self):
        return self.get("program")

    def set_program(self, program):
        self.set("program", json.dumps(program))

    @property
    def active(self):
        return self.get("active") == "1"

    @property
    def current_temp(self):
        return self.get("current_temp")

    @property
    def current_setting(self):
        return self.get("current_setting")

    @property
    def minutes_left(self):
        return self.get("minutes_left")

    @property
    def run_times(self):
        # a list of timestamps when experiments were started
        return self.hkeys("history")

    def get_history(self, timestamp):
        return self.hget("history", timestamp)


class Output(object):
    PWM_PIN = 36
    ENABLE_PIN = 38
    HERTZ = 1.0  # the response is super slow so 1 Hz is fine

    def __init__(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(Output.ENABLE_PIN, GPIO.OUT)
        GPIO.setup(Output.PWM_PIN, GPIO.OUT)
        # Sets up a PWM pin with 1 second cycles
        self._pwm = GPIO.PWM(Output.PWM_PIN, Output.HERTZ)
        self._pwm.start(0)

    def enable(self):
        GPIO.output(Output.ENABLE_PIN, GPIO.HIGH)

    def disable(self):
        GPIO.output(Output.ENABLE_PIN, GPIO.LOW)

    def set_pwm(self, new_duty_cycle):
        assert 0.0 <= new_duty_cycle <= 100.0
        self._pwm.ChangeDutyCycle(new_duty_cycle)


class TemperatureProbe(object):
    GPIO_ADDRESS = 0x4d

    def __init__(self):
        self._bus = smbus.SMBus(1)
        log.debug("Connected to SMBus")

    @property
    def current_temperature(self):
        data = self._bus.read_i2c_block_data(TemperatureProbe.GPIO_ADDRESS, 1, 2)
        return ((data[0] * 256) + data[1]) / 5.0


class TemperatureController(object):
    def __init__(self, probe=None, output=None, data_provider=None):
        self._probe = probe
        self._output = output
        self._data_provider = data_provider
        self._pid = PID()
        if self._data_provider:
            # Disable the temperature controller on boot to ensure we're not running an old program
            self._data_provider.deactivate()
        self._program = None
        self._history_key = None
        self._start_time = None

    @property
    def start_time(self):
        if self._start_time is None:
            self._start_time = datetime.utcnow()
        return self._start_time

    def run(self):
        while True:
            self._listen()
            self._activate()
            self._run_program()

    def _listen(self):
        """
        Do nothing until someone instructs the temperature controller to start running.

        """
        while True:
            if self._data_provider.active:
                break
            else:
                log.debug("Temperature controller inactive.")
                time.sleep(1)

    def _activate(self):
        self._program = TemperatureProgram()
        # get the program currently in Redis
        self._program.load_json(self._data_provider.program)
        # save the current timestamp so we can label data for the current run
        self._history_key = self._get_history_key()
        self._program.start()

    def _run_program(self):
        # Activate the motor driver chip, but ensure the heater won't get hot until we want it to
        self._output.set_pwm(0.0)
        self._output.enable()

        while True:
            if not self._data_provider.active:
                # Turn off the heater and return to listening mode
                log.debug("The program is inactive, according to the data provider.")
                self._output.disable()
                break
            else:
                # We're still running the program. Update the PID and adjust the duty cycle accordingly
                temperature = self._update_temperature()
                desired_temperature = self._program.get_desired_temperature()
                if desired_temperature is False:
                    # the program is over
                    break
                log.debug("Desired temp: %s" % desired_temperature)
                self._pid.update_set_point(desired_temperature)
                new_duty_cycle = self._pid.update(temperature)
                self._output.set_pwm(new_duty_cycle)
            time.sleep(1.0)

    def _update_temperature(self):
        temperature = self._probe.current_temperature
        log.debug("Current temp: %s" % temperature)
        timestamp = self.start_time - datetime.utcnow()
        self._data_provider.update_temperature(temperature, self._history_key, timestamp)
        return temperature

    def _get_history_key(self):
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


class TemperatureSetting(object):
    def __init__(self, temperature, duration_in_seconds):
        self._temperature = temperature
        self._duration = duration_in_seconds

    @property
    def temperature(self):
        return self._temperature

    @property
    def duration(self):
        return self._duration


class TemperatureProgram(object):
    def __init__(self):
        self._settings = []
        self._looping = False
        self._start = None
        self._hold_temp = None
        self._total_duration = 0.0

    @property
    def minutes_left(self):
        seconds_left = max(self._total_duration - time.time() + self._start, 0)
        log.debug("seconds left: %s" % seconds_left)
        return int(seconds_left / 60.0)

    def load_json(self, json_program):
        """
        json_program will be a dict like:
        {"1": {"mode": "set", "temperature": 60.0, "duration": 3600},"2": {"mode": "set", "temperature": 37.0, "duration": 600},"3": {"mode": "repeat", "num_repeats": 20},"4": {"mode": "hold", "temperature": 25.0}}
        Modes and attributes supported:

        set: temperature, duration
        repeat: num_repeats
        hold: temperature

        Modes and attributes planned:
        linear: temperature, duration
        exponential: temperature, duration

        :param json_program:    temperature settings for an experiment
        :type json_program:     str

        """
        action = {"set": self.set_temperature,
                  "repeat": self.repeat,
                  "hold": self.hold
                  }
        raw_program = json.loads(json_program)
        for index, parameters in sorted(raw_program.items(), key=lambda x: int(x[0])):
            # Get the mode and remove it from the parameters
            mode = parameters.pop("mode", None)
            # Run the desired action using the parameters given
            # Parameters of methods must match the keys exactly!
            log.debug("Adding instruction: %s with parameters: %s" % (mode, parameters))
            action[mode](**parameters)

    def set_temperature(self, temperature=25.0, duration=60):
        if not self._looping:
            setting = TemperatureSetting(temperature, duration)
            self._total_duration += duration
            self._settings.append(setting)
        return self

    def repeat(self, num_repeats=3):
        if not self._looping:
            new_settings = []
            for i in range(num_repeats):
                for action in self._settings:
                    new_settings.append(action)
                    self._total_duration += action.duration
            self._settings = new_settings
        return self

    def hold(self, temperature=25.0):
        self._hold_temp = temperature
        return self

    def start(self):
        assert self._start is None
        self._start = time.time()

    def get_desired_temperature(self):
        if self._looping:
            settings = cycle(self._settings)
        else:
            settings = self._settings
        elapsed = time.time() - self._start
        for setting in settings:
            elapsed -= setting.duration
            if elapsed < 0:
                return setting.temperature
        # The program program is over or holding at a specified temperature.
        return self._hold_temp if self._hold_temp else False


class PID:
    ROOM_TEMP = 20

    def __init__(self, kp=5.0, ki=1.0):
        self._kp = kp
        self._ki = ki
        self._previous_errors = []
        self._accumulated_error = 0.0
        self._accumulated_error_max = 20
        self._accumulated_error_min = -20
        self._set_point = 25.0

    def update(self, current_temperature):
        error = self.set_point - current_temperature
        log.debug("Error: %s" % error)
        # self._update_previous_errors(current_temperature, error)
        self._update_accumulated_error(error)
        p = self._kp * error
        log.debug("Proportional: %s" % p)
        i = self._accumulated_error * self._ki
        log.debug("Integral: %s" % i)
        total = 100 * int(p + i) / (self.set_point - PID.ROOM_TEMP + 5.0)
        log.debug("PI total: %s" % total)
        duty_cycle = max(0, min(100, total))
        log.info("Duty cycle: %s" % duty_cycle)
        return duty_cycle

    def _update_accumulated_error(self, error):
        # Add the current error to the accumulated error
        self._accumulated_error += error
        # Ensure the value is within the allowed limits
        self._accumulated_error = min(self._accumulated_error, self._accumulated_error_max)
        self._accumulated_error = max(self._accumulated_error, self._accumulated_error_min)
        log.debug("Accumulated error: %s" % self._accumulated_error)

    @property
    def set_point(self):
        return self._set_point

    def update_set_point(self, temperature):
        log.debug("Setting set point to %s" % temperature)
        room_temp_diff = temperature - PID.ROOM_TEMP + 5.0
        self._accumulated_error_max = room_temp_diff / 2.0
        self._accumulated_error_min = -room_temp_diff / 2.0
        self._set_point = float(temperature)