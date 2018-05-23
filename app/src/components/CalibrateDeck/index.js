// @flow
import * as React from 'react'
import {connect} from 'react-redux'
import {push, goBack} from 'react-router-redux'
import {Switch, Route, withRouter} from 'react-router'

import type {State, Dispatch} from '../../types'
import type {OP, SP, DP, CalibrateDeckProps, CalibrationStep} from './types'

import {getPipette} from '@opentrons/shared-data'

import {
  home,
  startDeckCalibration,
  deckCalibrationCommand,
  setCalibrationJogStep,
  getCalibrationJogStep,
  makeGetDeckCalibrationCommandState,
  makeGetDeckCalibrationStartState
} from '../../http-api-client'

import ClearDeckAlert from './ClearDeckAlert'
import InUseModal from './InUseModal'
import NoPipetteModal from './NoPipetteModal'
import ErrorModal from './ErrorModal'
import InstructionsModal from './InstructionsModal'
import ExitAlertModal from './ExitAlertModal'

const RE_STEP = '(1|2|3|4|5|6)'
const BAD_PIPETTE_ERROR = 'Unexpected pipette response from robot; please contact support'

export default withRouter(
  connect(makeMapStateToProps, mapDispatchToProps)(CalibrateDeck)
)

function CalibrateDeck (props: CalibrateDeckProps) {
  const {startRequest, pipetteProps, parentUrl, match: {path}} = props

  if (pipetteProps && !pipetteProps.pipette) {
    return (
      <ErrorModal
        closeUrl={parentUrl}
        error={{name: 'BadData', message: BAD_PIPETTE_ERROR}}
      />
    )
  }

  return (
    <Switch>
      <Route path={path} exact render={() => {
        const {error} = startRequest

        if (error) {
          const {status} = error

          // conflict: token already issued
          if (status === 409) {
            return (<InUseModal {...props} />)
          }

          // forbidden: no pipette attached
          if (status === 403) {
            return (<NoPipetteModal {...props}/>)
          }

          // props are generic in case we decide to reuse
          return (<ErrorModal closeUrl={parentUrl} error={error} />)
        }

        if (pipetteProps && pipetteProps.pipette) {
          return (<ClearDeckAlert {...props} {...pipetteProps} />)
        }

        return null
      }} />
      <Route path={`${path}/step-:step${RE_STEP}`} render={(stepProps) => {
        if (!pipetteProps || !pipetteProps.pipette) return null

        const {match: {params, url: stepUrl}} = stepProps
        const step: CalibrationStep = (params.step: any)
        const exitUrl = `${stepUrl}/exit`

        const startedProps = {
          ...props,
          exitUrl,
          pipette: pipetteProps.pipette,
          mount: pipetteProps.mount,
          calibrationStep: step
        }

        return (
          <div>
            <InstructionsModal {...startedProps} />
            <Route path={exitUrl} render={() => (
              <ExitAlertModal {...props} />
            )} />
          </div>
        )
      }} />
    </Switch>
  )
}

function makeMapStateToProps () {
  const getDeckCalCommand = makeGetDeckCalibrationCommandState()
  const getDeckCalStartState = makeGetDeckCalibrationStartState()

  return (state: State, ownProps: OP): SP => {
    const {robot} = ownProps
    const startRequest = getDeckCalStartState(state, robot)
    const pipetteInfo = startRequest.response && startRequest.response.pipette
    const pipetteProps = pipetteInfo
      ? {mount: pipetteInfo.mount, pipette: getPipette(pipetteInfo.model)}
      : null

    if (pipetteProps && !pipetteProps.pipette) {
      console.error('Invalid pipette received from API', pipetteInfo)
    }

    return {
      startRequest,
      pipetteProps,
      commandRequest: getDeckCalCommand(state, robot),
      jogStep: getCalibrationJogStep(state)
    }
  }
}

function mapDispatchToProps (dispatch: Dispatch, ownProps: OP): DP {
  const {robot, parentUrl} = ownProps

  return {
    jog: (axis, direction, step) => dispatch(
      deckCalibrationCommand(robot, {command: 'jog', axis, direction, step})
    ),
    onJogStepSelect: (event) => {
      const step = Number(event.target.value)
      dispatch(setCalibrationJogStep(step))
    },
    forceStart: () => dispatch(startDeckCalibration(robot, true)),
    // exit button click in title bar, opens exit alert modal, confirm exit click
    exit: () => dispatch(home(robot))
      .then(() => dispatch(deckCalibrationCommand(robot, {command: 'release'})))
      .then(() => dispatch(push(parentUrl))),
    // cancel button click in exit alert modal
    back: () => dispatch(goBack())
  }
}
