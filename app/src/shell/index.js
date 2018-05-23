// @flow
// desktop shell module
import {remote, ipcRenderer} from 'electron'
import {createSelector, type Selector} from 'reselect'

import createLogger from '../logger'

import type {State, Action, Middleware, ThunkPromiseAction, Error} from '../types'

const {
  CURRENT_VERSION,
  checkForUpdates,
  downloadUpdate,
  quitAndInstall
} = remote.require('./update')

const {
  getConfig
} = remote.require('./config')

const log = createLogger(__filename)

// TODO(mc, 2018-03-29): update sub reducer
type ShellState = {
  update: {
    checkInProgress: boolean,
    downloadInProgress: boolean,
    available: ?string,
    downloaded: boolean,
    error: ?Error,
  },
}

const INITIAL_STATE: ShellState = {
  update: {
    checkInProgress: false,
    downloadInProgress: false,
    available: null,
    downloaded: false,
    error: null
  }
}

type StartUpdateCheckAction = {|
  type: 'shell:START_UPDATE_CHECK'
|}

type FinishUpdateCheckAction = {|
  type: 'shell:FINISH_UPDATE_CHECK',
  payload: {|
    available: ?string,
    error: ?Error,
  |}
|}

type StartDownloadAction = {|
  type: 'shell:START_DOWNLOAD'
|}

type FinishDownloadAction = {|
  type: 'shell:FINISH_DOWNLOAD',
  payload: {|
    error: ?Error,
  |}
|}

export type ShellAction =
  | StartUpdateCheckAction
  | FinishUpdateCheckAction
  | StartDownloadAction
  | FinishDownloadAction

export function checkForShellUpdates (): ThunkPromiseAction {
  return (dispatch) => {
    dispatch({type: 'shell:START_UPDATE_CHECK'})

    return checkForUpdates()
      .then((info: {updateAvailable: boolean, version: string}) => ({
        available: info.updateAvailable ? info.version : null,
        error: null
      }))
      .catch((error: Error) => ({error}))
      .then((payload) => dispatch({
        type: 'shell:FINISH_UPDATE_CHECK',
        payload
      }))
  }
}

export function downloadShellUpdate (): ThunkPromiseAction {
  // TODO(mc, 2018-03-29): verify that update is available via state first
  return (dispatch) => {
    dispatch({type: 'shell:START_DOWNLOAD'})

    return downloadUpdate()
      .then(() => ({error: null}))
      .catch((error: Error) => ({error}))
      .then((payload) => dispatch({type: 'shell:FINISH_DOWNLOAD', payload}))
  }
}

export function quitAndInstallShellUpdate () {
  quitAndInstall()
}

export function shellReducer (
  state: ?ShellState,
  action: Action
): ShellState {
  if (!state) return INITIAL_STATE

  switch (action.type) {
    case 'shell:START_UPDATE_CHECK':
      return {...state, update: {...state.update, checkInProgress: true}}

    case 'shell:FINISH_UPDATE_CHECK':
      return {
        ...state,
        update: {...state.update, ...action.payload, checkInProgress: false}}

    case 'shell:START_DOWNLOAD':
      return {...state, update: {...state.update, downloadInProgress: true}}

    case 'shell:FINISH_DOWNLOAD':
      return {
        ...state,
        update: {
          ...state.update,
          ...action.payload,
          downloadInProgress: false,
          downloaded: !action.payload.error
        }
      }
  }

  return state
}

export const shellMiddleware: Middleware = (store) => {
  const {dispatch} = store

  ipcRenderer.on('dispatch', (_, action) => {
    log.debug('Received action from main via IPC', {action})
    dispatch(action)
  })

  return (next) => (action) => {
    if (action.meta && action.meta.shell) ipcRenderer.send('dispatch', action)

    return next(action)
  }
}

export type ShellUpdate = $PropertyType<ShellState, 'update'> & {
  current: string
}

export const getShellUpdate: Selector<State, void, ShellUpdate> =
  createSelector(
    selectShellUpdateState,
    (updateState) => ({...updateState, current: CURRENT_VERSION})
  )

// getShellConfig makes a sync RPC call, so use sparingly
export function getShellConfig () {
  return getConfig()
}

function selectShellUpdateState (state: State) {
  return state.shell.update
}
