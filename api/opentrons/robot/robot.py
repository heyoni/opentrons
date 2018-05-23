import os
import logging
from functools import lru_cache

import opentrons.util.calibration_functions as calib
from numpy import add, subtract
from opentrons import commands, containers, drivers
from opentrons.instruments import pipette_config
from opentrons.broker import subscribe
from opentrons.containers import Container
from opentrons.data_storage import database, old_container_loading,\
    database_migration
from opentrons.drivers.smoothie_drivers import driver_3_0
from opentrons.drivers.rpi_drivers import gpio
from opentrons.robot.mover import Mover
from opentrons.robot.robot_configs import load
from opentrons.trackers import pose_tracker
from opentrons.config import feature_flags as fflags
from opentrons.instruments.pipette_config import Y_OFFSET_MULTI

log = logging.getLogger(__name__)

TIP_CLEARANCE_DECK = 20    # clearance when moving between different labware
TIP_CLEARANCE_LABWARE = 5  # clearance when staying within a single labware


class InstrumentMosfet(object):
    """
    Provides access to MagBead's MOSFET.
    """

    def __init__(self, this_robot, mosfet_index):
        self.robot = this_robot
        self.mosfet_index = mosfet_index

    def engage(self):
        """
        Engages the MOSFET.
        """
        self.robot._driver.set_mosfet(self.mosfet_index, True)

    def disengage(self):
        """
        Disengages the MOSFET.
        """
        self.robot._driver.set_mosfet(self.mosfet_index, False)

    def wait(self, seconds):
        """
        Pauses protocol execution.

        Parameters
        ----------
        seconds : int
            Number of seconds to pause for.
        """
        self.robot._driver.wait(seconds)


class InstrumentMotor(object):
    """
    Provides access to Robot's head motor.
    """

    def __init__(self, this_robot, axis):
        self.robot = this_robot
        self.axis = axis

    def move(self, value, mode='absolute'):
        """
        Move plunger motor.

        Parameters
        ----------
        value : int
            A one-dimensional coordinate to move to.
        mode : {'absolute', 'relative'}
        """
        kwargs = {self.axis: value}
        self.robot._driver.move_plunger(
            mode=mode, **kwargs
        )

    def home(self):
        """
        Home plunger motor.
        """
        self.robot._driver.home(self.axis)

    def wait(self, seconds):
        """
        Wait.

        Parameters
        ----------
        seconds : int
            Number of seconds to pause for.
        """
        self.robot._driver.wait(seconds)

    def speed(self, rate):
        """
        Set motor speed.

        Parameters
        ----------
        rate : int
        """
        self.robot._driver.set_plunger_speed(rate, self.axis)
        return self


def _setup_container(container_name):
    try:
        container = database.load_container(container_name)

    # Database.load_container throws ValueError when a container name is not
    # found.
    except ValueError:
        # First must populate "get persisted container" list
        old_container_loading.load_all_containers_from_disk()
        # Load container from old json file
        container = old_container_loading.get_persisted_container(
            container_name)
        # Rotate coordinates to fit the new deck map
        rotated_container = database_migration.rotate_container_for_alpha(
            container)
        # Save to the new database
        database.save_new_container(rotated_container, container_name)

    container.properties['type'] = container_name
    container_x, container_y, container_z = container._coordinates

    if not fflags.split_labware_definitions():
        # infer z from height
        if container_z == 0 and 'height' in container[0].properties:
            container_z = container[0].properties['height']

    from opentrons.util.vector import Vector
    container._coordinates = Vector(
        container_x,
        container_y,
        container_z)

    return container


# NOTE: modules are stored in the Containers db table
def _setup_module(module):
    x, y, z = database.load_module(module.name)
    from opentrons.util.vector import Vector
    module._coordinates = Vector(x, y, z)
    return module


class Robot(object):
    """
    This class is the main interface to the robot.

    Through this class you can can:
        * define your :class:`opentrons.Deck`
        * :meth:`connect` to Opentrons physical robot
        * :meth:`home` axis, move head (:meth:`move_to`)
        * :meth:`pause` and :func:`resume` the protocol run
        * set the :meth:`head_speed` of the robot

    Each Opentrons protocol is a Python script. When evaluated the script
    creates an execution plan which is stored as a list of commands in
    Robot's command queue.

    Here are the typical steps of writing the protocol:
        * Using a Python script and the Opentrons API load your
          containers and define instruments
          (see :class:`~opentrons.instruments.pipette.Pipette`).
        * Call :meth:`reset` to reset the robot's state and clear commands.
        * Write your instructions which will get converted
          into an execution plan.
        * Review the list of commands generated by a protocol
          :meth:`commands`.
        * :meth:`connect` to the robot and call :func:`run` it on a real robot.

    See :class:`Pipette` for the list of supported instructions.
    """

    def __init__(self, config=None):
        """
        Initializes a robot instance.

        Notes
        -----
        This class is a singleton. That means every time you call
        :func:`__init__` the same instance will be returned. There's
        only once instance of a robot.
        """
        self.config = config or load()
        self._driver = driver_3_0.SmoothieDriver_3_0_0(config=self.config)
        self.modules = []
        self.fw_version = self._driver.get_fw_version()

        self.INSTRUMENT_DRIVERS_CACHE = {}
        self.model_by_mount = {'left': None, 'right': None}

        # TODO (artyom, 09182017): once protocol development experience
        # in the light of Session concept is fully fleshed out, we need
        # to properly communicate deprecation of commands. For now we'll
        # leave it as is for compatibility with documentation.
        self._commands = []
        self._unsubscribe_commands = None
        self.reset()

    def _get_placement_location(self, placement):
        location = None
        # If `placement` is a string, assume it is a slot
        if isinstance(placement, str):
            location = self._deck[placement]
        elif getattr(placement, 'stackable', False):
            location = placement
        return location

    def _is_available_slot(self, location, share, slot, container_name):
        if pose_tracker.has_children(self.poses, location) and not share:
            raise RuntimeWarning(
                'Slot {0} has child. Use "containers.load(\'{1}\', \'{2}\', share=True)"'.format(  # NOQA
                    slot, container_name, slot))
        else:
            return True

    def reset(self):
        """
        Resets the state of the robot and clears:
            * Deck
            * Instruments
            * Command queue
            * Runtime warnings

        Examples
        --------

        >>> from opentrons import robot # doctest: +SKIP
        >>> robot.reset() # doctest: +SKIP
        """
        self._actuators = {
            'left': {
                'carriage': Mover(
                    driver=self._driver,
                    src=pose_tracker.ROOT,
                    dst=id(self.config.gantry_calibration),
                    axis_mapping={'z': 'Z'}),
                'plunger': Mover(
                    driver=self._driver,
                    src=pose_tracker.ROOT,
                    dst='volume-calibration-left',
                    axis_mapping={'x': 'B'})
            },
            'right': {
                'carriage': Mover(
                    driver=self._driver,
                    src=pose_tracker.ROOT,
                    dst=id(self.config.gantry_calibration),
                    axis_mapping={'z': 'A'}),
                'plunger': Mover(
                    driver=self._driver,
                    src=pose_tracker.ROOT,
                    dst='volume-calibration-right',
                    axis_mapping={'x': 'C'})
            }
        }

        self.poses = pose_tracker.init()

        self._runtime_warnings = []

        self._deck = containers.Deck()
        self._fixed_trash = None
        self.setup_deck()
        self.setup_gantry()
        self._instruments = {}

        self._use_safest_height = False

        self._previous_instrument = None
        self._prev_container = None

        # TODO: Move homing info to driver
        self.axis_homed = {
            'x': False, 'y': False, 'z': False, 'a': False, 'b': False}

        self.clear_commands()

        # update the position of each Mover
        self._driver.update_position()
        for mount in self._actuators.values():
            for mover in mount.values():
                self.poses = mover.update_pose_from_driver(self.poses)
        return self

    def cache_instrument_models(self):
        for mount in self.model_by_mount.keys():
            self.model_by_mount[mount] = self._driver.read_pipette_model(mount)

    def turn_on_button_light(self):
        '''
        This method is called by a script in the docker container,
        so for backwards compatibility with old containers, this method is
        staying and being used as on "on-boot/loading" color, or it can just
        be ignored in the future
        '''
        gpio.set_button_color('white')  # white while the server is starting...

    def turn_on_rail_lights(self):
        gpio.set_high(gpio.OUTPUT_PINS['FRAME_LEDS'])

    def turn_off_rail_lights(self):
        gpio.set_low(gpio.OUTPUT_PINS['FRAME_LEDS'])

    def identify(self, seconds):
        """
        Identify a robot by flashing the light around the frame button for 10s
        """
        from time import sleep
        for i in range(seconds):
            gpio.set_button_color('off')
            sleep(0.25)
            gpio.set_button_color('blue')
            sleep(0.25)

    def setup_gantry(self):
        driver = self._driver

        left_carriage = self._actuators['left']['carriage']
        right_carriage = self._actuators['right']['carriage']

        left_plunger = self._actuators['left']['plunger']
        right_plunger = self._actuators['right']['plunger']

        self.gantry = Mover(
            driver=driver,
            axis_mapping={'x': 'X', 'y': 'Y'},
            src=pose_tracker.ROOT,
            dst=id(self.config.gantry_calibration)
        )

        # Extract only transformation component
        inverse_transform = pose_tracker.inverse(
            pose_tracker.extract_transform(self.config.gantry_calibration))

        self.poses = pose_tracker.bind(self.poses) \
            .add(
                obj=id(self.config.gantry_calibration),
                transform=self.config.gantry_calibration) \
            .add(obj=self.gantry, parent=id(self.config.gantry_calibration)) \
            .add(obj=left_carriage, parent=self.gantry) \
            .add(obj=right_carriage, parent=self.gantry) \
            .add(
                obj='left',
                parent=left_carriage,
                transform=inverse_transform) \
            .add(
                obj='right',
                parent=right_carriage,
                transform=inverse_transform) \
            .add(obj='volume-calibration-left') \
            .add(obj='volume-calibration-right') \
            .add(obj=left_plunger, parent='volume-calibration-left') \
            .add(obj=right_plunger, parent='volume-calibration-right')

    def add_instrument(self, mount, instrument):
        """
        Adds instrument to a robot.

        Parameters
        ----------
        mount : str
            Specifies which axis the instruments is attached to.
            Valid options are "left" or "right".
        instrument : Instrument
            An instance of a :class:`Pipette` to attached to the axis.

        Notes
        -----
        A canonical way to add to add a Pipette to a robot is:

        ::

            from opentrons import instruments
            m300 = instruments.P300_Multi(mount='left')

        This will create a pipette and call :func:`add_instrument`
        to attach the instrument.
        """
        if mount in self._instruments:
            prev_instr = self._instruments[mount]
            raise RuntimeError('Instrument {0} already on {1} mount'.format(
                prev_instr.name, mount))
        self._instruments[mount] = instrument
        self.cache_instrument_models()
        instrument.instrument_actuator = self._actuators[mount]['plunger']
        instrument.instrument_mover = self._actuators[mount]['carriage']

        # instrument_offset is the distance found (with tip-probe) between
        # the pipette's expected position, and the actual position
        # this is expected to be no greater than ~3mm
        # Z is not included, because Z offsets found during tip-probe are used
        # to determined the tip's length
        cx, cy, _ = self.config.instrument_offset[mount][instrument.type]

        # model_offset is the expected position of the pipette, determined
        # by designed dimensions of that model (eg: p10-multi vs p300-single)
        mx, my, mz = instrument.model_offset

        # combine each offset to get the pipette's position relative to gantry
        _x, _y, _z = (
            mx + cx,
            my + cy,
            mz
        )
        # if it's the left mount, apply the offset from right pipette
        if mount == 'left':
            _x, _y, _z = (
               _x + self.config.mount_offset[0],
               _y + self.config.mount_offset[1],
               _z + self.config.mount_offset[2]
            )
        self.poses = pose_tracker.add(
            self.poses,
            instrument,
            parent=mount,
            point=(_x, _y, _z)
        )

    def remove_instrument(self, mount):
        instrument = self._instruments.pop(mount, None)
        if instrument:
            self.poses = pose_tracker.remove(self.poses, instrument)
        self.cache_instrument_models()

    def add_warning(self, warning_msg):
        """
        Internal. Add a runtime warning to the queue.
        """
        self._runtime_warnings.append(warning_msg)

    def get_warnings(self):
        """
        Get current runtime warnings.

        Returns
        -------

        Runtime warnings accumulated since the last :func:`run`
        or :func:`simulate`.
        """
        return list(self._runtime_warnings)

    def get_motor(self, axis):
        """
        Get robot's head motor.

        Parameters
        ----------
        axis : {'a', 'b'}
            Axis name. Please check stickers on robot's gantry for the name.
        """
        instr_type = 'instrument'
        key = (instr_type, axis)

        motor_obj = self.INSTRUMENT_DRIVERS_CACHE.get(key)
        if not motor_obj:
            motor_obj = InstrumentMotor(self, axis)
            self.INSTRUMENT_DRIVERS_CACHE[key] = motor_obj
        return motor_obj

    def connect(self, port=None, options=None):
        """
        Connects the robot to a serial port.

        Parameters
        ----------
        port : str
            OS-specific port name or ``'Virtual Smoothie'``
        options : dict
            if :attr:`port` is set to ``'Virtual Smoothie'``, provide
            the list of options to be passed to :func:`get_virtual_device`

        Returns
        -------
        ``True`` for success, ``False`` for failure.

        Note
        ----
        If you wish to connect to the robot without using the OT App, you will
        need to use this function.

        Examples
        --------

        >>> from opentrons import robot # doctest: +SKIP
        >>> robot.connect() # doctest: +SKIP
        """

        self._driver.connect(port=port)
        for module in self.modules:
            module.connect()
        self.fw_version = self._driver.get_fw_version()

    def _update_axis_homed(self, *args):
        for a in args:
            for letter in a:
                if letter.lower() in self.axis_homed:
                    self.axis_homed[letter.lower()] = True

    def home(self, *args, **kwargs):
        """
        Home robot's head and plunger motors.

        Parameters
        ----------
        *args :
            A string with axes to home. For example ``'xyz'`` or ``'ab'``.

            If no arguments provided home Z-axis then X, Y, B, A

        Notes
        -----
        Sometimes while executing a long protocol,
        a robot might accumulate precision
        error and it is recommended to home it. In this scenario, add
        ``robot.home('xyzab')`` into your script.
        """

        # Home gantry first to avoid colliding with labware
        # and to make sure tips are not in the liquid while
        # homing plungers. Z/A axis will automatically home before X/Y
        self.poses = self.gantry.home(self.poses)
        # Then plungers
        self.poses = self._actuators['left']['plunger'].home(self.poses)
        self.poses = self._actuators['right']['plunger'].home(self.poses)

        # next move should not use any previously used instrument or labware
        # to prevent robot.move_to() from using risky path optimization
        self._previous_instrument = None
        self._prev_container = None

        # explicitly update carriage Mover positions in pose tree
        # because their Mover.home() commands aren't used here
        for a in self._actuators.values():
            self.poses = a['carriage'].update_pose_from_driver(self.poses)

    def move_head(self, *args, **kwargs):
        self.poses = self.gantry.move(self.poses, **kwargs)

    def head_speed(
            self, combined_speed=None,
            x=None, y=None, z=None, a=None, b=None, c=None):
        """
        Set the speeds (mm/sec) of the robot

        Parameters
        ----------
        speed : number setting the current combined-axes speed
        combined_speed : number specifying a combined-axes speed
        <axis> : key/value pair, specifying the maximum speed of that axis

        Examples
        ---------

        >>> from opentrons import robot # doctest: +SKIP
        >>> robot.reset() # doctest: +SKIP
        >>> robot.head_speed(300) # doctest: +SKIP
        #  default axes speed is 300 mm/sec
        >>> robot.head_speed(combined_speed=400) # doctest: +SKIP
        #  default speed is 400 mm/sec
        >>> robot.head_speed(x=400, y=200) # doctest: +SKIP
        # sets max speeds of X and Y
        """
        user_set_speeds = {'x': x, 'y': y, 'z': z, 'a': a, 'b': b, 'c': c}
        axis_max_speeds = {
            axis: value
            for axis, value in user_set_speeds.items()
            if value
        }
        if axis_max_speeds:
            self._driver.set_axis_max_speed(axis_max_speeds)
        if combined_speed:
            self._driver.set_speed(combined_speed)

    def move_to(
            self,
            location,
            instrument,
            strategy='arc',
            **kwargs):
        """
        Move an instrument to a coordinate, container or a coordinate within
        a container.

        Parameters
        ----------
        location : one of the following:
            1. :class:`Placeable` (i.e. Container, Deck, Slot, Well) — will
            move to the origin of a container.
            2. :class:`Vector` move to the given coordinate in Deck coordinate
            system.
            3. (:class:`Placeable`, :class:`Vector`) move to a given coordinate
            within object's coordinate system.

        instrument :
            Instrument to move relative to. If ``None``, move relative to the
            center of a gantry.

        strategy : {'arc', 'direct'}
            ``arc`` : move to the point using arc trajectory
            avoiding obstacles.

            ``direct`` : move to the point in a straight line.
        """

        placeable, coordinates = containers.unpack_location(location)

        # because the top position is what is tracked,
        # this checks if coordinates doesn't equal top
        offset = subtract(coordinates, placeable.top()[1])

        if 'trough' in repr(placeable):
            # Move the pipette so that a multi-channel pipette is centered in
            # the trough well to prevent crashing into the side, which would
            # happen if you send the "A1" tip to the center of the well. See
            # `robot.calibrate_container_with_instrument` for corresponding
            # offset and comment.
            offset = offset + (0, Y_OFFSET_MULTI, 0)

        if isinstance(placeable, containers.WellSeries):
            placeable = placeable[0]

        target = add(
            pose_tracker.absolute(
                self.poses,
                placeable
            ),
            offset.coordinates
        )

        if self._previous_instrument:
            if self._previous_instrument != instrument:
                self._previous_instrument.retract()
                # because we're switching pipettes, this ensures a large (safe)
                # Z arc height will be used for the new pipette
                self._prev_container = None

        self._previous_instrument = instrument

        if strategy == 'arc':
            arc_coords = self._create_arc(instrument, target, placeable)
            for coord in arc_coords:
                self.poses = instrument._move(
                    self.poses,
                    **coord)

        elif strategy == 'direct':
            position = {'x': target[0], 'y': target[1], 'z': target[2]}
            self.poses = instrument._move(
                self.poses,
                **position)
        else:
            raise RuntimeError(
                'Unknown move strategy: {}'.format(strategy))

    def _create_arc(self, inst, destination, placeable=None):
        """
        Returns a list of coordinates to arrive to the destination coordinate
        """
        this_container = None
        if isinstance(placeable, containers.Well):
            this_container = placeable.get_parent()
        elif isinstance(placeable, containers.WellSeries):
            this_container = placeable.get_parent()
        elif isinstance(placeable, containers.Container):
            this_container = placeable

        if this_container and self._prev_container == this_container:
            # movements that stay within the same container do not need to
            # avoid other containers on the deck, so the travel height of
            # arced movements can be relative to just that one container
            arc_top = self.max_placeable_height_on_deck(this_container)
            arc_top += TIP_CLEARANCE_LABWARE
        elif self._use_safest_height:
            # bring the pipettes up as high as possible while calibrating
            arc_top = inst._max_deck_height()
        else:
            # bring pipette up above the tallest container currently on deck
            arc_top = self.max_deck_height() + TIP_CLEARANCE_DECK

        self._prev_container = this_container

        # if instrument is currently taller than arc_top, don't move down
        _, _, pip_z = pose_tracker.absolute(self.poses, inst)

        arc_top = max(arc_top, destination[2], pip_z)
        arc_top = min(arc_top, inst._max_deck_height())

        strategy = [
            {'z': arc_top},
            {'x': destination[0], 'y': destination[1]},
            {'z': destination[2]}
        ]

        return strategy

    def disconnect(self):
        """
        Disconnects from the robot.
        """
        if self._driver:
            self._driver.disconnect()

        for module in self.modules:
            module.disconnect()

        self.axis_homed = {
            'x': False, 'y': False, 'z': False, 'a': False, 'b': False}

    def get_deck_slot_types(self):
        return 'slots'

    def get_slot_offsets(self):
        """
        col_offset
        - from bottom left corner of 1 to bottom corner of 2

        row_offset
        - from bottom left corner of 1 to bottom corner of 4

        TODO: figure out actual X and Y offsets (from origin)
        """
        SLOT_OFFSETS = {
            'slots': {
                'col_offset': 132.50,
                'row_offset': 90.5
            }
        }
        slot_settings = SLOT_OFFSETS.get(self.get_deck_slot_types())
        row_offset = slot_settings.get('row_offset')
        col_offset = slot_settings.get('col_offset')
        return (row_offset, col_offset)

    def get_max_robot_rows(self):
        # TODO: dynamically figure out robot rows
        return 4

    def get_max_robot_cols(self):
        # TODO: dynamically figure out robot cols
        return 3

    def add_slots_to_deck(self):
        row_offset, col_offset = self.get_slot_offsets()
        row_count = self.get_max_robot_rows()
        col_count = self.get_max_robot_cols()

        for row_index in range(row_count):
            for col_index in range(col_count):
                properties = {
                    'width': col_offset,
                    'length': row_offset,
                    'height': 0
                }
                slot = containers.Slot(properties=properties)
                slot_coordinates = (
                    (col_offset * col_index),
                    (row_offset * row_index),
                    0
                )
                slot_index = col_index + (row_index * col_count)
                slot_name = str(slot_index + 1)
                self._deck.add(slot, slot_name, slot_coordinates)

    def setup_deck(self):
        self.add_slots_to_deck()

        # Setup Deck as root object for pose tracker
        self.poses = pose_tracker.add(
            self.poses,
            self._deck
        )

        for slot in self._deck:
            self.poses = pose_tracker.add(
                self.poses,
                slot,
                self._deck,
                pose_tracker.Point(*slot._coordinates)
            )

        # @TODO (Laura & Andy) Slot and type of trash
        # needs to be pulled from config file
        if fflags.short_fixed_trash():
            self._fixed_trash = self.add_container('fixed-trash', '12')
        else:
            self._fixed_trash = self.add_container('tall-fixed-trash', '12')

    @property
    def deck(self):
        return self._deck

    @property
    def fixed_trash(self):
        return self._fixed_trash

    def get_instruments_by_name(self, name):
        res = []
        for k, v in self.get_instruments():
            if v.name == name:
                res.append((k, v))

        return res

    def get_instruments(self, name=None):
        """
        :returns: sorted list of (mount, instrument)
        """
        if name:
            return self.get_instruments_by_name(name)

        return sorted(
            self._instruments.items(), key=lambda s: s[0].lower())

    def get_containers(self):
        """
        Returns all containers currently on the deck.
        """
        return self._deck.containers()

    def add_container(self, name, slot, label=None, share=False):
        container = _setup_container(name)
        if container is not None:
            location = self._get_placement_location(slot)
            if self._is_available_slot(location, share, slot, name):
                location.add(container, label or name)
            self.add_container_to_pose_tracker(location, container)
            self.max_deck_height.cache_clear()
        return container

    def add_module(self, module, slot, label=None):
        module = _setup_module(module)
        location = self._get_placement_location(slot)
        location.add(module, label or module.__class__.__name__)
        self.modules.append(module)
        self.poses = pose_tracker.add(
            self.poses,
            module,
            location,
            pose_tracker.Point(*module._coordinates))

    def add_container_to_pose_tracker(self, location, container: Container):
        """
        Add container and child wells to pose tracker. Sets container.parent
        (slot) as pose tracker parent
        """
        self.poses = pose_tracker.add(
            self.poses,
            container,
            container.parent,
            pose_tracker.Point(*container._coordinates))

        for well in container:
            center_x, center_y, center_z = well.top()[1]
            offset_x, offset_y, offset_z = well._coordinates
            if not fflags.split_labware_definitions():
                center_z = 0
            self.poses = pose_tracker.add(
                self.poses,
                well,
                container,
                pose_tracker.Point(
                    center_x + offset_x,
                    center_y + offset_y,
                    center_z + offset_z
                )
            )

    @commands.publish.both(command=commands.pause)
    def pause(self):
        """
        Pauses execution of the protocol. Use :meth:`resume` to resume
        """
        self._driver.pause()

    def stop(self):
        """
        Stops execution of the protocol. (alias for `halt`)
        """
        self.halt()

    @commands.publish.both(command=commands.resume)
    def resume(self):
        """
        Resume execution of the protocol after :meth:`pause`
        """
        self._driver.resume()

    def halt(self):
        """
        Stops execution of both the protocol and the Smoothie board immediately
        """
        self._driver.kill()
        self.reset()
        self.home()

    def get_attached_pipettes(self):
        """
        Gets model names of attached pipettes

        :return: :dict with keys 'left' and 'right' and a model string for each
            mount, or 'uncommissioned' if no model string available
        """
        left_data = {
                'mount_axis': 'z',
                'plunger_axis': 'b',
                'model': self.model_by_mount['left'],
            }
        left_model = left_data.get('model')
        if left_model:
            tip_length = pipette_config.configs[left_model].tip_length
            left_data.update({'tip_length': tip_length})

        right_data = {
                'mount_axis': 'a',
                'plunger_axis': 'c',
                'model': self.model_by_mount['right']
            }
        right_model = right_data.get('model')
        if right_model:
            tip_length = pipette_config.configs[right_model].tip_length
            right_data.update({'tip_length': tip_length})
        return {
            'left': left_data,
            'right': right_data
        }

    def get_serial_ports_list(self):
        ports = []
        # TODO: Store these settings in config
        if os.environ.get('ENABLE_VIRTUAL_SMOOTHIE', '').lower() == 'true':
            ports = [drivers.VIRTUAL_SMOOTHIE_PORT]
        ports.extend(drivers.get_serial_ports_list())
        return ports

    def is_connected(self):
        if not self._driver:
            return False
        return self._driver.is_connected()

    def is_simulating(self):
        if not self._driver:
            return False
        return self._driver.simulating

    @commands.publish.before(command=commands.comment)
    def comment(self, msg):
        pass

    def commands(self):
        return self._commands

    def clear_commands(self):
        self._commands.clear()
        if self._unsubscribe_commands:
            self._unsubscribe_commands()

        def on_command(message):
            payload = message.get('payload')
            text = payload.get('text')
            if text is None:
                return

            if message['$'] == 'before':
                self._commands.append(text.format(**payload))

        self._unsubscribe_commands = subscribe(
            commands.types.COMMAND, on_command)

    def calibrate_container_with_instrument(self,
                                            container: Container,
                                            instrument,
                                            save: bool
                                            ):
        '''Calibrates a container using the bottom of the first well'''
        well = container[0]

        # Get the relative position of well with respect to instrument
        delta = pose_tracker.change_base(
            self.poses,
            src=instrument,
            dst=well
        )

        if fflags.calibrate_to_bottom():

            delta_x = delta[0]
            delta_y = delta[1]
            if 'tiprack' in container.get_type():
                delta_z = delta[2]
            else:
                delta_z = delta[2] + well.z_size()
        else:
            delta_x = delta[0]
            delta_y = delta[1]
            delta_z = delta[2]

        if 'trough' in container.get_type():
            # Rather than calibrating troughs to the center of the well, we
            # calibrate such that a multi-channel would be centered in the
            # well. We don't differentiate between single- and multi-channel
            # pipettes here, and we track the tip of a multi-channel pipette
            # that would go into well A1 of an 8xN plate rather than the axial
            # center, but the axial center of a well is what we track for
            # calibration, so we add Y_OFFSET_MULTI in the calibration `move`
            # command, and then back that value off of the pipette position
            # here (Y_OFFSET_MULTI is the y-distance from the axial center of
            # the pipette to the A1 tip).
            delta_y = delta_y - Y_OFFSET_MULTI

        self.poses = calib.calibrate_container_with_delta(
            self.poses,
            container,
            delta_x,
            delta_y,
            delta_z,
            save
        )

        self.max_deck_height.cache_clear()

    @lru_cache()
    def max_deck_height(self):
        return pose_tracker.max_z(self.poses, self._deck)

    def max_placeable_height_on_deck(self, placeable):
        """
        :param placeable:
        :return: Calibrated height of container in mm from
        deck as the reference point
        """
        offset = placeable.top()[1]
        placeable_coordinate = add(
            pose_tracker.absolute(
                self.poses,
                placeable
            ),
            offset.coordinates
        )
        placeable_tallest_point = pose_tracker.max_z(self.poses, placeable)
        return placeable_coordinate[2] + placeable_tallest_point
