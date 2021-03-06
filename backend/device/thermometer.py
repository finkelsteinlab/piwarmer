import logging
import math
import os

# The temperature sensor sometimes erroneously reports temperatures between -100 and -200 degrees
# To be safe, we ignore any results that are less than 10 degrees since the room will never get that
# cold. You can override this limit by setting the environment variable MINIMUM_BELIEVABLE_TEMPERATURE.
MINIMUM_BELIEVABLE_TEMPERATURE = float(os.getenv('MINIMUM_BELIEVABLE_TEMPERATURE', 10.0))
log = logging.getLogger("heater." + __name__)


class Thermometer(object):
    """
    Gets temperature values from a temperature probe and reports them.

    """
    def __init__(self, sensor):
        self._sensor = sensor
        log.debug("Successfully connected to temperature probe.")

    @property
    def current_temperature(self):
        """
        The chip that we use, the MAX31855, doesn't always return a numeric value - sometimes it returns NaN
        (that's right: NaN, not None) for unknown reasons. To get around this, we just keep reading until
        we get a number back. This hasn't been a problem since the periods where it returns NaN usually only
        last for a few milliseconds and are relatively sparse.

        Note that this method is blocking when you're using the actual chip! If you unplug the probe, the
        entire script will halt until you plug it back in.

        :rtype:     float

        """
        temperature = float('NaN')
        while math.isnan(temperature) or temperature < MINIMUM_BELIEVABLE_TEMPERATURE:
            temperature = float(self._sensor.readTempC())
        return temperature
