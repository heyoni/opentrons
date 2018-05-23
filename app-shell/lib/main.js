// electron main entry point
'use strict'

const {app, dialog, ipcMain, Menu} = require('electron')

const createUi = require('./ui')
const initializeMenu = require('./menu')
const {initialize: initializeApiUpdate} = require('./api-update')
const createLogger = require('./log')
const {getConfig, getStore, getOverrides, registerConfig} = require('./config')

const config = getConfig()
const log = createLogger(__filename)

log.debug('App config', {
  config,
  store: getStore(),
  overrides: getOverrides()
})

if (config.devtools) {
  require('electron-debug')({showDevTools: true})
}

// hold on to references so they don't get garbage collected
let mainWindow
let rendererLogger

app.on('ready', startUp)

function startUp () {
  log.info('Starting App')
  process.on('uncaughtException', (error) => log.error('Uncaught: ', {error}))

  mainWindow = createUi()
  rendererLogger = createRendererLogger()

  initializeMenu()

  initializeApiUpdate()
    .catch((error) => log.error('Initialize API update module error', error))

  // wire modules to UI dispatches
  const dispatch = (action) => {
    log.debug('Sending action via IPC to renderer', {action})
    mainWindow.webContents.send('dispatch', action)
  }

  const configHandler = registerConfig(dispatch)

  ipcMain.on('dispatch', (_, action) => {
    log.debug('Received action via IPC from renderer', {action})
    configHandler(action)
  })

  if (config.devtools) {
    installAndOpenExtensions()
      .catch((error) => dialog.showErrorBox('Error opening dev tools', error))
  }

  log.silly('Global references', {mainWindow, rendererLogger})
}

function createRendererLogger () {
  log.info('Creating renderer logger')

  const logger = createLogger()
  ipcMain.on('log', (_, info) => logger.log(info))

  return logger
}

function installAndOpenExtensions () {
  const devtools = require('electron-devtools-installer')
  const forceDownload = !!process.env.UPGRADE_EXTENSIONS
  const install = devtools.default
  const extensions = [
    'REACT_DEVELOPER_TOOLS',
    'REDUX_DEVTOOLS'
  ]

  return Promise
    .all(extensions.map((name) => install(devtools[name], forceDownload)))
    .then(() => mainWindow.webContents.on('context-menu', (_, props) => {
      const {x, y} = props

      Menu
        .buildFromTemplate([{
          label: 'Inspect element',
          click: () => mainWindow.inspectElement(x, y)
        }])
        .popup(mainWindow)
    }))
}
