from http.server import HTTPServer, BaseHTTPRequestHandler
from random import randint
from threading import Thread
from tempfile import mkstemp
import logging
import json
import os
from SimpleWebSocketServer import SimpleWebSocketServer, WebSocket
import neovim
from neovim.api.nvim import NvimError

buffer_handler_map = {}
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)


class GhostWebSocketHandler(WebSocket):

    def handleMessage(self):
        req = json.loads(self.data)
        logger.info("recd on websocket: %s message: %s",
                    self.address, req["text"])
        self.server.context.onMessage(req, self)

    def handleConnected(self):
        logger.debug("Websocket connected %s", self.address)

    def handleClose(self):
        logger.debug("Websocket closed event %s ", self.address)
        self.server.context.onWebSocketClose(self)


class MyWebSocketServer(SimpleWebSocketServer):

    def __init__(self, context, *args, **kwargs):
        self.context = context
        SimpleWebSocketServer.__init__(self, *args, **kwargs)


def startWebSocketSvr(context, port):
    websocket_server = MyWebSocketServer(context, '', port,
                                         GhostWebSocketHandler)
    ws_thread = Thread(target=websocket_server.serveforever, daemon=True)
    ws_thread.start()


class WebRequestHandler(BaseHTTPRequestHandler):

    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

    def do_GET(self):
        self._set_headers()
        port = randint(60000, 65535)
        response_obj = {"ProtocolVersion": 1}
        response_obj["WebSocketPort"] = port
        startWebSocketSvr(self.server.context, port)
        self.wfile.write(json.dumps(response_obj).encode())


class MyHTTPServer(HTTPServer):

    def __init__(self, context, *args, **kwargs):
        self.context = context
        HTTPServer.__init__(self, *args, **kwargs)


@neovim.plugin
class Ghost(object):

    def __init__(self, vim):
        self.nvim = vim
        self.server_started = False

    @neovim.command('GhostStart', range='', nargs='0', sync=True)
    def server_start(self, args, range):
        if self.server_started:
            self.nvim.command("echo 'Ghost server already running'")
            logger.info("server already running")
            return

        self.httpserver = MyHTTPServer(self, ('', 4001), WebRequestHandler)
        http_server_thread = Thread(target=self.httpserver.serve_forever,
                                    daemon=True)
        http_server_thread.start()
        self.server_started = True
        logger.info("server started")
        self.nvim.command("echo 'Ghost server started'")

    @neovim.command('GhostStop', range='', nargs='0', sync=True)
    def server_stop(self, args, range):
        if not self.server_started:
            self.nvim.command("echo 'Server not running'")
            return
        self.server_started = False
        self.httpserver.shutdown()
        self.httpserver.socket.close()
        self.nvim.command("echo 'Ghost server stopped'")

    @neovim.function("GhostNotify")
    def ghostSend(self, args):
        logger.info(args)
        event, bufnr = args
        if bufnr not in buffer_handler_map:
            return
        wsclient, req = buffer_handler_map[bufnr]
        self.nvim.command("echo 'event recd command %s, %s'" % (event, bufnr))
        if event == "text_changed":
            logger.info("sending message to client ")
            text = "\n".join(self.nvim.buffers[bufnr][:])
            req["text"] = text
            # self.nvim.command("echo '%s'" % text)
            wsclient.sendMessage(json.dumps(req))
        elif event == "closed":
            logger.info(("Calling _handleOnWebSocketClose"
                         " in response to buffer"
                         " %d closure in nvim", bufnr))
            self._handle_web_socket_close(wsclient)

    def _handle_on_message(self, req, websocket):
        try:
            if websocket in buffer_handler_map:
                # existing buffer
                bufnr, fh = buffer_handler_map[websocket]
                # delete textchanged autocmds otherwise we'll get a loop
                logger.info("delete buffer changed autocmd")
                self.nvim.command("au! TextChanged,TextChangedI <buffer=%d>" %
                                  bufnr)
                self.nvim.buffers[bufnr][:] = req["text"].split("\n")
            else:
                # new client
                temp_file_handle, temp_file_name = mkstemp(suffix=".txt",
                                                           text=True)
                self.nvim.command("ed %s" % temp_file_name)
                self.nvim.current.buffer[:] = req["text"].split("\n")
                bufnr = self.nvim.current.buffer.number
                delete_cmd = ("au BufDelete <buffer> call"
                              " GhostNotify('closed', %d)" % bufnr)
                buffer_handler_map[bufnr] = [websocket, req]
                buffer_handler_map[websocket] = [bufnr, temp_file_handle]
                self.nvim.command(delete_cmd)
                logger.debug("Set up aucmd: %s", delete_cmd)

            change_cmd = ("au TextChanged,TextChangedI <buffer> call"
                          " GhostNotify('text_changed', %d)" % bufnr)
            self.nvim.command(change_cmd)
            logger.debug("Set up aucmd: %s", change_cmd)
        except Exception as ex:
            logger.error("Caught exception handling message: %s", ex)
            self.nvim.command("echo '%s'" % ex)

    def onMessage(self, req, websocket):
        self.nvim.async_call(self._handle_on_message, req, websocket)
        # self.nvim.command("echo 'connected direct'")
        return

    def _handle_web_socket_close(self, websocket):
        logger.debug("Cleaning up on websocket close")
        if websocket not in buffer_handler_map:
            logger.warn("websocket closed but no matching buffer found")
            return

        bufnr, fh = buffer_handler_map[websocket]
        bufFilename = self.nvim.buffers[bufnr].name
        try:
            self.nvim.command("bdelete! %d" % bufnr)
        except NvimError as nve:
            logger.error("Error while deleting buffer %s", nve)

        try:
            os.close(fh)
            os.remove(bufFilename)
            logger.debug("Deleted file %s and removed buffer %d", bufFilename,
                         bufnr)
        except OSError as ose:
            logger.error("Error while closing & deleting file %s", ose)

        buffer_handler_map.pop(bufnr, None)
        buffer_handler_map.pop(websocket, None)
        websocket.close()
        logger.debug("Websocket closed")

    def onWebSocketClose(self, websocket):
        self.nvim.async_call(self._handle_web_socket_close, websocket)
