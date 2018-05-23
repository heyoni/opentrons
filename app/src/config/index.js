// @flow
// config redux module
import {setIn} from '@thi.ng/paths'
import {getShellConfig} from '../shell'
import type {State, Action} from '../types'
import type {LogLevel} from '../logger'

type UrlProtocol = 'file:' | 'http:'

// TODO(mc, 2018-05-17): put this type somewhere common to app and app-shell
export type Config = {
  devtools: boolean,

  // logging config
  log: {
    level: {
      file: LogLevel,
      console: LogLevel
    }
  },

  // ui and browser config
  ui: {
    width: number,
    height: number,
    url: {
      protocol: UrlProtocol,
      path: string
    },
    webPreferences: {
      webSecurity: boolean
    }
  }
}

type UpdateConfigAction = {|
  type: 'config:UPDATE',
  payload: {|
    path: string,
    value: any
  |},
  meta: {|
    shell: true
  |}
|}

type SetConfigAction = {|
  type: 'config:UPDATE',
  payload: {|
    path: string,
    value: any
  |}
|}

export type ConfigAction = UpdateConfigAction | SetConfigAction

// trigger a config value update to the app-shell via shell middleware
export function updateConfig (path: string, value: any): UpdateConfigAction {
  return {type: 'config:UPDATE', payload: {path, value}, meta: {shell: true}}
}

// config reducer
export function configReducer (
  state: ?Config,
  action: Action
): Config {
  // initial state
  // getShellConfig makes a sync RPC call, so use sparingly
  if (!state) return getShellConfig()

  switch (action.type) {
    case 'config:SET':
      return setIn(state, action.payload.path, action.payload.value)
  }

  return state
}

export function getConfig (state: State): Config {
  return state.config
}
