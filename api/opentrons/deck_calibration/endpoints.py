from aiohttp import web
from uuid import uuid1
from opentrons.instruments import pipette_config
from opentrons import instruments, robot
from opentrons.robot import robot_configs
from opentrons.deck_calibration import jog, position, dots_set, z_pos
from opentrons.deck_calibration.linal import add_z, solve
from typing import Dict, Tuple

import logging
import json

session = None
log = logging.getLogger(__name__)


def expected_points():
    slot_1_lower_left,\
        slot_3_lower_right,\
        slot_7_upper_left = dots_set()

    return {
        '1': slot_1_lower_left,
        '2': slot_3_lower_right,
        '3': slot_7_upper_left}


def safe_points() -> Dict[str, Tuple[int, int, int]]:
    # Safe points are defined as 5mm toward the center of the deck in x, y and
    # 10mm above the deck. User is expect to jog to the critical point from the
    # corresponding safe point, to avoid collision depending on direction of
    # misalignment between the deck and the gantry.
    slot_1_lower_left, \
        slot_3_lower_right, \
        slot_7_upper_left = expected_points().values()
    slot_1_safe_point = (
        slot_1_lower_left[0] + 5, slot_1_lower_left[1] + 5, 10)
    slot_3_safe_point = (
        slot_3_lower_right[0] - 5, slot_3_lower_right[1] + 5, 10)
    slot_7_safe_point = (
        slot_7_upper_left[0] + 5, slot_7_upper_left[1] - 5, 10)
    attach_tip_point = (200, 90, 150)

    return {
        '1': slot_1_safe_point,
        '2': slot_3_safe_point,
        '3': slot_7_safe_point,
        'safeZ': z_pos,
        'attachTip': attach_tip_point
    }


def _get_uuid() -> str:
    return str(uuid1())


class SessionManager:
    """
    Creates a session manager to handle all commands required for factory
    calibration.
    Before issuing a movement command, the following must be done:
    1. Create a session manager
    2. Initialize a pipette
    3. Select the current pipette
    """
    def __init__(self):
        self.id = _get_uuid()
        self.pipettes = {}
        self.current_mount = None
        self.current_model = None
        self.tip_length = None
        self.points = {k: None for k in expected_points().keys()}
        self.z_value = None

        default = robot_configs._build_config({}, {}).gantry_calibration
        robot.config = robot.config._replace(gantry_calibration=default)


# -------------- Route Fns -----------------------------------------------
# Note: endpoints should not call these functions directly, to ensure that
# session protections are applied--should be called through the dispatch
# endpoint
# ------------------------------------------------------------------------
def init_pipette():
    """
    Finds pipettes attached to the robot currently and chooses the correct one
    to add to the session.

    :return: The pipette type and mount chosen for deck calibration
    """
    global session
    pipette_info = set_current_mount(robot.get_attached_pipettes())
    pipette = pipette_info['pipette']
    res = {}
    if pipette:
        session.current_model = pipette_info['model']
        session.pipettes[pipette.mount] = pipette
        res = {'mount': pipette.mount, 'model': pipette_info['model']}

    log.info("Pipette info {}".format(session.pipettes))

    return res


def set_current_mount(attached_pipettes):
    """
    Choose the pipette in which to execute commands. If there is no pipette,
    or it is uncommissioned, the pipette is not mounted.

    :attached_pipettes attached_pipettes: Information obtained from the current
    pipettes attached to the robot. This looks like the following:
    :dict with keys 'left' and 'right' and a model string for each
    mount, or 'uncommissioned' if no model string available
    :return: The selected pipette
    """
    global session
    left = attached_pipettes.get('left')
    right = attached_pipettes.get('right')
    left_pipette = None
    right_pipette = None

    pipette = None
    model = None

    if left['model'] in pipette_config.configs.keys():
        pip_config = pipette_config.load(left['model'])
        left_pipette = instruments._create_pipette_from_config(
            mount='left', config=pip_config)

    if right['model'] in pipette_config.configs.keys():
        pip_config = pipette_config.load(right['model'])
        right_pipette = instruments._create_pipette_from_config(
            mount='right', config=pip_config)

    if right_pipette and right_pipette.channels == 1:
        session.current_mount = 'A'
        pipette = right_pipette
        model = right['model']
    elif left_pipette and left_pipette.channels == 1:
        session.current_mount = 'Z'
        pipette = left_pipette
        model = left['model']
    else:
        if right_pipette:
            session.current_mount = 'A'
            pipette = right_pipette
            model = right['model']
        elif left_pipette:
            session.current_mount = 'Z'
            pipette = left_pipette
            model = left['model']
    return {'pipette': pipette, 'model': model}


async def attach_tip(data):
    """
    Attach a tip to the current pipette

    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'attach tip'
      'tipLength': a float representing how much the length of a pipette
        increases when a tip is added
    }
    """
    global session
    tip_length = data.get('tipLength')
    mount = 'left' if session.current_mount == 'Z' else 'right'
    pipette = session.pipettes[mount]

    if not tip_length:
        message = 'Error: "tipLength" must be specified in request'
        status = 400
    else:
        if pipette.tip_attached:
            log.warning('attach tip called while tip already attached')
            pipette._remove_tip(pipette._tip_length)

        session.tip_length = tip_length
        pipette._add_tip(tip_length)
        message = "Tip length set: {}".format(tip_length)
        status = 200

    return web.json_response({'message': message}, status=status)


async def detach_tip(data):
    """
    Detach the tip from the current pipette

    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'detach tip'
    }
    """
    global session
    mount = 'left' if session.current_mount == 'Z' else 'right'
    pipette = session.pipettes[mount]

    if not pipette.tip_attached:
        log.warning('detach tip called with no tip')

    pipette._remove_tip(session.tip_length)
    session.tip_length = None

    return web.json_response({'message': "Tip removed"}, status=200)


async def run_jog(data):
    """
    Allow the user to jog the selected pipette around the deck map

    :param data: Information obtained from a POST request.
    The content type is application/json
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'jog'
      'axis': The current axis you wish to move
      'direction': The direction you wish to move (+ or -)
      'step': The increment you wish to move
    }
    :return: The position you are moving to based on axis, direction, step
    given by the user.
    """
    axis = data.get('axis')
    direction = data.get('direction')
    step = data.get('step')

    if axis not in ('x', 'y', 'z'):
        message = '"axis" must be "x", "y", or "z"'
        status = 400
    elif direction not in (-1, 1):
        message = '"direction" must be -1 or 1'
        status = 400
    elif step is None:
        message = '"step" must be specified'
        status = 400
    else:
        if axis == 'z':
            axis = session.current_mount
        # print("=---> Jogging {} {}".format(axis, direction * step))
        position = jog(axis.upper(), direction, step)
        message = 'Jogged to {}'.format(position)
        status = 200

    return web.json_response({'message': message}, status=status)


async def move(data):
    """
    Allow the user to move the selected pipette to a specific point

    :param data: Information obtained from a POST request.
    The content type is application/json
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'move'
      'point': The name of the point to move to. Must be one of
               ["1", "2", "3", "safeZ", "attachTip"]
    }
    :return: The position you are moving to
    """
    point_name = data.get('point')
    point = safe_points().get(point_name)

    if point and len(point) == 3:
        mount = 'left' if session.current_mount == 'Z' else 'right'
        pipette = session.pipettes[mount]

        # For multichannel pipettes, we use the tip closest to the front of the
        # robot rather than the back (this is the tip that would go into well
        # H1 of a plate when pipetting from the first row of a 96 well plate,
        # for instance). Since moves are issued for the A1 tip, we have to
        # adjust the target point by 2 * Y_OFFSET_MULTI (where the offset value
        # is the distance from the axial center of the pipette to the A1 tip).
        # By sending the A1 tip to to the adjusted target, the H1 tip should
        # go to the desired point. Y_OFFSET_MULT must then be backed out of xy
        # positions saved in the `save_xy` handler (not 2 * Y_OFFSET_MULTI,
        # because the axial center of the pipette will only be off by
        # 1* Y_OFFSET_MULTI).
        if not pipette.channels == 1:
            x = point[0]
            y = point[1] + pipette_config.Y_OFFSET_MULTI * 2
            z = point[2]
            point = (x, y, z)

        pipette.move_to((robot.deck, point), strategy='arc')
        message = 'Moved to {}'.format(point)
        status = 200
    else:
        message = '"point" must be one of "1", "2", "3", "safeZ", "attachTip"'
        status = 400

    return web.json_response({'message': message}, status=status)


async def save_xy(data):
    """
    Save the current XY values for the calibration data

    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'save xy'
      'point': a string ID ['1', '2', or '3'] of the calibration point to save
    }
    """
    global session
    valid_points = list(session.points.keys())
    point = data.get('point')
    if point not in valid_points:
        message = 'point must be one of {}'.format(valid_points)
        status = 400
    elif not session.current_mount:
        message = "Mount must be set before calibrating"
        status = 400
    else:
        x, y, z = position(session.current_mount)
        mount = 'left' if session.current_mount == 'Z' else 'right'
        if mount == 'left':
            dx, dy, dz = robot.config.mount_offset
            x = x + dx
            y = y + dy
            z = z + dz
        if session.pipettes[mount].channels != 1:
            # See note in `move`
            y = y - pipette_config.Y_OFFSET_MULTI
        session.points[point] = (x, y)
        message = "Saved point {} value: {}".format(
            point, session.points[point])
        status = 200
    return web.json_response({'message': message}, status=status)


async def save_z(data):
    """
    Save the current Z height value for the calibration data

    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'save z'
    }
    """
    if not session.tip_length:
        message = "Tip length must be set before calibrating"
        status = 400
    else:
        actual_z = position(session.current_mount)[-1]
        length_offset = pipette_config.load(
            session.current_model).model_offset[-1]
        session.z_value = actual_z - session.tip_length + length_offset
        message = "Saved z: {}".format(session.z_value)
        status = 200
    return web.json_response({'message': message}, status=status)


async def save_transform(data):
    """
    Calculate the transormation matrix that calibrates the gantry to the deck
    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'save transform'
    }
    """
    if any([v is None for v in session.points.values()]):
        message = "Not all points have been saved"
        status = 400
    else:
        # expected values based on mechanical drawings of the robot
        expected_pos = expected_points()
        expected = [
            expected_pos[p] for p in expected_pos.keys()]
        # measured data
        actual = [session.points[p] for p in sorted(session.points.keys())]
        # Generate a 2 dimensional transform matrix from the two matricies
        flat_matrix = solve(expected, actual)
        # Add the z component to form the 3 dimensional transform
        calibration_matrix = add_z(flat_matrix, session.z_value)

        robot.config = robot.config._replace(
            gantry_calibration=list(
                map(lambda i: list(i), calibration_matrix)))

        robot_configs.save_deck_calibration(robot.config)
        robot_configs.backup_configuration(robot.config)
        message = "Config file saved and backed up"
        status = 200
    return web.json_response({'message': message}, status=status)


async def release(data):
    """
    Release a session

    :param data: Information obtained from a POST request.
    The content type is application/json.
    The correct packet form should be as follows:
    {
      'token': UUID token from current session start
      'command': 'release'
    }
    """
    global session
    session = None
    robot.remove_instrument('left')

    robot.remove_instrument('right')
    return web.json_response({"message": "calibration session released"})

# ---------------------- End Route Fns -------------------------

# Router must be defined after all route functions
router = {'jog': run_jog,
          'move': move,
          'save xy': save_xy,
          'attach tip': attach_tip,
          'detach tip': detach_tip,
          'save z': save_z,
          'save transform': save_transform,
          'release': release}


async def start(request):
    """
    Begins the session manager for factory calibration, if a session is not
    already in progress, or if the "force" key is specified in the request. To
    force, use the following body:
    {
      "force": true
    }
    :return: The current session ID token or an error message
    """
    global session

    try:
        body = await request.json()
    except json.decoder.JSONDecodeError:
        # Body will be null for requests without parameters (normal operation)
        log.debug("No body in {}".format(request))
        body = {}

    if not session or body.get('force'):
        if body.get('force'):
            robot.remove_instrument('left')
            robot.remove_instrument('right')
        session = SessionManager()
        res = init_pipette()
        if res:
            status = 201
            data = {'token': session.id, 'pipette': res}
        else:
            session = None
            status = 403
            data = {'message': 'Error, pipette not recognized'}
    else:
        data = {'message': 'Error, session in progress. Use "force" key in'
                           ' request body to override'}
        status = 409
    return web.json_response(data, status=status)


async def dispatch(request):
    """
    Routes commands to subhandlers based on the command field in the body.
    """
    if session:
        message = ''
        data = await request.json()
        try:
            log.info("Dispatching {}".format(data))
            _id = data.get('token')
            if not _id:
                message = '"token" field required for calibration requests'
                raise AssertionError
            command = data.get('command')
            if not command:
                message = '"command" field required for calibration requests'
                raise AssertionError

            if _id == session.id:
                res = await router[command](data)
            else:
                res = web.json_response(
                    {'message': 'Invalid token: {}'.format(_id)}, status=403)
        except AssertionError:
            res = web.json_response({'message': message}, status=400)
        except Exception as e:
            res = web.json_response(
                {'message': 'Exception {} raised by dispatch of {}: {}'.format(
                    type(e), data, e)},
                status=500)
    else:
        res = web.json_response(
            {'message': 'Session must be started before issuing commands'},
            status=418)
    return res
