// sets up the main window ui
'use strict'

const {app, BrowserWindow} = require('electron')
const path = require('path')
const config = require('./config').getConfig('ui')
const log = require('./log')(__filename)

const urlPath = config.url.protocol === 'file:'
  ? path.join(app.getAppPath(), config.url.path)
  : config.url.path

const url = `${config.url.protocol}//${urlPath}`

const WINDOW_OPTS = {
  show: false,
  useContentSize: true,
  width: config.width,
  height: config.height,
  // allow webPreferences to be set at launchtime from config
  webPreferences: Object.assign({
    // TODO(mc, 2018-05-15): turn off experimentalFeatures?
    experimentalFeatures: true,

    // TODO(mc, 2018-05-15): disable nodeIntegration in renderer thread
    nodeIntegration: true,
    // node integration needed for mdns robot discovery in webworker
    nodeIntegrationInWorker: true
  }, config.webPreferences)
}

module.exports = function createUi () {
  log.debug('Creating main window', {options: WINDOW_OPTS})

  const mainWindow = new BrowserWindow(WINDOW_OPTS)
    .once('ready-to-show', () => {
      log.debug('Main window ready to show')
      mainWindow.show()
    })

  log.info(`Loading ${url}`)
  mainWindow.loadURL(url, {'extraHeaders': 'pragma: no-cache\n'})

  return mainWindow
}
