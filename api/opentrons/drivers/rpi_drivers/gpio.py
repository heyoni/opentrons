from os import system
from sys import platform
"""
Raspberry Pi GPIO control module

Read/Write from RPi GPIO pins is performed by exporting a pin by number,
writing the direction for the pin, and then writing a high or low signal
to the pin value.

To export a pin, find the desired pin in OUTPUT_PINS or INPUT_PINS, and write
the corresponding number to `/sys/class/gpio/export`.

After export, set pin direction by writing either "in" or "out" to
`/sys/class/gpio/gpio<pin_number>/direction`.

After direction is set, set a pin high by writing a "1" or set the pin low by
writing "0" (zero) to `/sys/class/gpio/gpio<pin_number>/value`.

This library abstracts those operations by providing an `initialize` function
to set up all pins correctly, and then providing `set_low` and `set_high`
functions that accept a pin number. The OUTPUT_PINS and INPUT_PINS dicts
provide pin-mappings so calling code does not need to use raw integers.
"""


IN = "in"
OUT = "out"

LOW = "0"
HIGH = "1"

# Note: in test pins are sorted by value, so listing them in that order here
#   makes it easier to read the tests. Pin numbers defined by bridge wiring
#   the Pi to the Smoothie.
OUTPUT_PINS = {
    'FRAME_LEDS': 6,
    'BLUE_BUTTON': 13,
    'HALT': 18,
    'GREEN_BUTTON': 19,
    'AUDIO_ENABLE': 21,
    'ISP': 23,
    'RESET': 24,
    'RED_BUTTON': 26
}

INPUT_PINS = {
    'BUTTON_INPUT': 5,
    'WINDOW_INPUT': 20
}

_path_prefix = "/sys/class/gpio"

BUTTON_COLORS = {
    'off': {'red': False, 'green': False, 'blue': False},
    'white': {'red': True, 'green': True, 'blue': True},
    'red': {'red': True, 'green': False, 'blue': False},
    'green': {'red': False, 'green': True, 'blue': False},
    'blue': {'red': False, 'green': False, 'blue': True},
    'yellow': {'red': True, 'green': True, 'blue': False},
    'pink': {'red': True, 'green': False, 'blue': True},
    'teal': {'red': False, 'green': True, 'blue': True}
}


def _enable_pin(pin, direction):
    """
    In order to enable a GPIO pin, the pin number must be written into
    /sys/class/gpio/export, and then the direction ("in" or "out" must be
    written to /sys/class/gpio/gpio<number>/direction

    :param pin: An integer corresponding to the GPIO number of the pin in RPi
      GPIO board numbering (not physical pin numbering)

    :param direction: "in" or "out"
    """
    _write_value(pin, "{}/export".format(_path_prefix))
    _write_value(direction, "{0}/gpio{1}/direction".format(_path_prefix, pin))


def _write_value(value, path):
    """
    Writes specified value into path. Note that the value is wrapped in single
    quotes in the command, to prevent injecting bash commands.
    :param value: The value to write (usually a number or string)
    :param path: A valid system path
    """
    base_command = "echo '{0}' > {1}"
    # There is no common method for redirecting stderr to a null sink, so the
    # command string is platform-dependent
    if platform == 'win32':
        command = "{0} > NUL".format(base_command)
    else:
        command = "exec 2> /dev/null; {0}".format(base_command)
    system(command.format(value, path))


def _read_value(path):
    """
    Reads value of specified path.
    :param path: A valid system path
    """
    read_value = 0
    if platform == 'win32':
        pass
    else:
        with open(path) as f:
            read_value = int(f.read())
    return read_value


def set_high(pin):
    """
    Sets a pin high by number. This pin must have been previously initialized
    and set up as with direction of OUT, otherwise this operation will not
    behave as expected.

    High represents "on" for lights, and represents normal running state for
    HALT and RESET pins.

    :param pin: An integer corresponding to the GPIO number of the pin in RPi
      GPIO board numbering (not physical pin numbering)
    """
    _write_value(HIGH, "{0}/gpio{1}/value".format(_path_prefix, pin))


def set_low(pin):
    """
    Sets a pin low by number. This pin must have been previously initialized
    and set up as with direction of OUT, otherwise this operation will not
    behave as expected.

    Low represents "off" for lights, and writing the RESET or HALT pins low
    will terminate Smoothie operation until written high again.

    :param pin: An integer corresponding to the GPIO number of the pin in RPi
      GPIO board numbering (not physical pin numbering)
    """
    _write_value(LOW, "{0}/gpio{1}/value".format(_path_prefix, pin))


def read(pin):
    """
    Reads a pin's value. This pin must have been previously initialized
    and set up as with direction of IN, otherwise this operation will not
    behave as expected.

    :param pin: An integer corresponding to the GPIO number of the pin in RPi
      GPIO board numbering (not physical pin numbering)
    """
    return _read_value("{0}/gpio{1}/value".format(_path_prefix, pin))


def _set_button_light(red=False, green=False, blue=False):
    color_pins = {
        OUTPUT_PINS['RED_BUTTON']: red,
        OUTPUT_PINS['GREEN_BUTTON']: green,
        OUTPUT_PINS['BLUE_BUTTON']: blue
    }
    for pin, state in color_pins.items():
        if state:
            set_high(pin)
        else:
            set_low(pin)


def set_button_color(color):
    if color not in BUTTON_COLORS:
        raise ValueError('Not a known button color: {}'.format(color))
    _set_button_light(**BUTTON_COLORS[color])


def initialize():
    """
    All named pins in OUTPUT_PINS and INPUT_PINS are exported, and set the
    HALT pin high (normal running state), since the default value after export
    is low.
    """
    for pin in sorted(OUTPUT_PINS.values()):
        _enable_pin(pin, OUT)

    for pin in sorted(INPUT_PINS.values()):
        _enable_pin(pin, IN)
