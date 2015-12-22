from abc import abstractmethod
import cycle
from datetime import datetime
import logging
import pid
import program
import time


log = logging.getLogger(__name__)


class BaseRunner(object):
    """
    These methods are implemented here in this base class because we foresee the possibility that people might
    want to have some kind of manual control mode, where they can adjust the temperature on the fly without a
    program. That has not yet been written though.

    """
    def __init__(self, current_state, thermometer, heater):
        self._current_state = current_state
        self._thermometer = thermometer
        self._heater = heater

    def run(self):
        while True:
            self._boot()
            self._listen()
            self._prerun()
            self._run()

    def __exit__(self, exc_type, exc_val, exc_tb):
        log.debug("Exiting program runner.")
        if exc_type:
            log.exception("Abnormal termination!")
        self._shutdown()

    def _shutdown(self):
        log.debug("Shutting down heater.")
        try:
            self._heater.disable()
        except:
            log.exception("DANGER! HEATER DID NOT SHUT DOWN")
        else:
            log.debug("Heater shutdown successful.")
        log.debug("Clearing API data.")
        try:
            self._current_state.clear()
        except:
            log.exception("Failed to clear API data!")
        else:
            log.debug("API data cleared.")

    def __enter__(self):
        return self

    def _boot(self):
        """
        Unsets the current program. We require that this runs before every loop to prevent accidentally starting a program whose parameters were somehow left in Redis.

        """
        log.info("Booting! About to clear API data")
        self._current_state.clear()

    def _listen(self):
        """
        Wait until the "active" key is on in Redis before starting a program.

        """
        while True:
            if self._current_state.active:
                log.info("We need to run a program!")
                break
            else:
                try:
                    # update the current temperature in Redis so that we can see how hot the heater is, even if we're not running a program
                    self._current_state.current_temp = self._thermometer.current_temperature
                except:
                    # absolutely do not allow this loop to terminate. Though if it did, supervisord would restart the process, but that's annoying and
                    # results in some downtime
                    log.exception("Something went wrong in the _listen() loop!")
                time.sleep(1)

    @abstractmethod
    def _prerun(self):
        """
        Things that need to be done before the run starts.

        """
        raise NotImplemented

    @abstractmethod
    def _run(self):
        """
        What to do when the heater should potentially be on.

        """
        raise NotImplemented


class ProgramRunner(BaseRunner):
    """
    Runs a pre-defined program, and ensures that shutdown

    """
    def __init__(self, current_state, thermometer, heater):
        super(ProgramRunner, self).__init__(current_state, thermometer, heater)
        self._start_time = None
        self._program = None
        self._accumulated_error = None
        self._pid = None
        self._temperature_log = None

    def _prerun(self):
        """
        Set up the PID for temperature control.

        """
        driver = self._current_state.driver
        driver = pid.Driver(driver['name'], driver['kp'], driver['ki'], driver['kd'],
                            driver['max_accumulated_error'], driver['min_accumulated_error'])
        self._pid = pid.PID(driver)
        self._accumulated_error = 0.0
        self._start_time = datetime.utcnow()
        log.info("Start time: %s" % self._start_time)
        self._temperature_log = self._get_temperature_log()
        self._program = program.TemperatureProgram(self._current_state.program)
        self._heater.enable()

    def _get_temperature_log(self):
        """
        Creates a machine-readable log of the temperature, the target temperature, and the duty cycle at 1-second intervals.

        """
        # Set up another logger for temperature logs
        temperature_log = logging.getLogger("temperatures")
        handler = logging.FileHandler('/var/log/piwarmer/temperature-%s.log' % self._start_time.strftime("%Y-%m-%d-%H-%M-%S"))
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        temperature_log.addHandler(handler)
        temperature_log.setLevel(logging.INFO)
        return temperature_log

    def _run(self):
        while True:
            assert self._start_time is not None
            assert self._current_state is not None
            assert self._thermometer is not None
            assert self._accumulated_error is not None

            # check if the user pressed the stop button
            if not self._current_state.active:
                log.info("The program has ended or been stopped")
                self._shutdown()
                break

            # make some safe assignments that should never fail
            current_cycle = cycle.CurrentCycle()
            current_cycle.accumulated_error = self._accumulated_error
            current_cycle.current_time = datetime.utcnow()
            current_cycle.start_time = self._start_time
            current_cycle.program = self._program

            if current_cycle.current_step is None:
                # the program is over and we're not using a Hold setting
                log.info("We're out of steps to run, so we should shut down now.")
                self._shutdown()
                break

            # I/O - read the temperature. This operation is blocking!
            current_cycle.current_temperature = self._thermometer.current_temperature

            # make calculations based on I/O having worked
            current_cycle.duty_cycle, self._accumulated_error = self._pid.update(current_cycle)

            # save the temperature information to a machine-readable log file
            self._temperature_log.info("%s\t%s\t%s" % (current_cycle.current_temperature,
                                                       current_cycle.desired_temperature,
                                                       current_cycle.duty_cycle))
            # physically activate the heater, if necessary
            self._heater.heat(current_cycle.duty_cycle)

            # update the API data so the frontend can know what's happening
            self._current_state.current_temp = current_cycle.current_temperature
            self._current_state.current_step = current_cycle.current_step
