import sublime
import sublime_plugin
import os
import tempfile
import socket
import subprocess
from time import strftime
from threading import Thread
try:
    import socketserver
except ImportError:
    import SocketServer as socketserver


FILES = {}
server = None


def subl(*args):
    executable_path = sublime.executable_path()
    if sublime.platform() == 'osx':
        app_path = executable_path[:executable_path.rfind('.app/') + 5]
        executable_path = app_path + 'Contents/SharedSupport/bin/subl'

    subprocess.Popen([executable_path] + list(args))

    def on_activated():
        if sublime.platform() == 'windows':
            # refocus sublime text window
            subprocess.Popen([executable_path, "--command", ""])
        window = sublime.active_window()
        view = window.active_view()
        sublime_plugin.on_activated(view.id())
        sublime_plugin.on_activated_async(view.id())

    sublime.set_timeout(on_activated, 100)


def say(msg):
    print('[remotesubl {}]: {}'.format(strftime("%H:%M:%S"), msg))


class File:
    def __init__(self, session):
        self.session = session
        self.env = {}
        self.data = b""
        self.ready = False
        self.temp_path = None

    def append(self, line):
        if len(self.data) < self.env["data"]:
            self.data += line

        if len(self.data) >= self.env["data"]:
            self.data = self.data[:self.env["data"]]
            self.ready = True

    def close(self, remove=True):
        self.session.socket.send(b"close\n")
        self.session.socket.send(b"token: " + self.env['token'].encode("utf8") + b"\n")
        self.session.socket.send(b"\n")
        if remove:
            os.unlink(self.temp_path)
            os.rmdir(self.temp_dir)
        self.session.try_close()

    def save(self):
        self.session.socket.send(b"save\n")
        self.session.socket.send(b"token: " + self.env['token'].encode("utf8") + b"\n")
        temp_file = open(self.temp_path, "rb")
        new_file = temp_file.read()
        temp_file.close()
        self.session.socket.send(b"data: " + str(len(new_file)).encode("utf8") + b"\n")
        self.session.socket.send(new_file)
        self.session.socket.send(b"\n")

    def get_temp_dir(self):
        # First determine if the file has been sent before.
        for f in FILES.values():
            if f.env["real-path"] == self.env["real-path"] and \
                    ":" in self.env["display-name"] and \
                    f.env["display-name"] == self.env["display-name"]:
                return f.temp_dir

        # Create a secure temporary directory, both for privacy and to allow
        # multiple files with the same basename to be edited at once without
        # overwriting each other.
        try:
            return tempfile.mkdtemp(prefix='remotesubl-')
        except OSError as e:
            sublime.error_message(
                'Failed to create remotesubl temporary directory! Error: {}'.format(e))

    def open(self):
        self.temp_dir = self.get_temp_dir()
        self.temp_path = os.path.join(
            self.temp_dir,
            os.path.basename(self.env['display-name'].split(':')[-1]))
        try:
            temp_file = open(self.temp_path, "wb+")
            temp_file.write(self.data)
            temp_file.flush()
            temp_file.close()
        except IOError as e:
            print(e)
            # Remove the file if it exists.
            if os.path.exists(self.temp_path):
                os.remove(self.temp_path)
            try:
                os.rmdir(self.temp_dir)
            except OSError:
                pass

            sublime.error_message('Failed to write to temp file! Error: %s' % str(e))

        # create new window if needed
        if len(sublime.windows()) == 0 or "new" in self.env:
            sublime.run_command("new_window")

        # Open it within sublime
        view = sublime.active_window().open_file(
            "{0}:{1}:0".format(
                self.temp_path, self.env['selection'] if 'selection' in self.env else 0),
            sublime.ENCODED_POSITION)

        # Add the file metadata to the view's settings
        # This is mostly useful to obtain the path of this file on the server
        view.settings().set('remotesubl', self.env)

        # if the current view is attahced to another file object,
        # that file object has to be closed first.
        if view.id() in FILES:
            file = FILES.pop(view.id())
            try:
                # connection may have lost
                file.close(remove=False)
            except:
                pass

        # Add the file to the global list
        FILES[view.id()] = self

        # Bring sublime to front by running `subl --command ""`
        subl("--command", "")
        view.run_command("remote_subl_update_status_bar")


class Session:
    def __init__(self, socket):
        self.socket = socket
        self.parsing_data = False
        self.nconn = 0
        self.file = None

    def parse_input(self, input_line):
        if input_line.strip() == b"open":
            self.file = File(self)
            self.nconn += 1
            return

        if self.parsing_data:
            self.file.append(input_line)
            if self.file.ready:
                self.file.open()
                self.parsing_data = False
                self.file = None
            return

        if not self.file:
            return

        # prase settings
        input_line = input_line.decode("utf8").strip()
        if ":" not in input_line:
            # not a setting
            return

        k, v = input_line.split(":", 1)

        if k == "data":
            self.file.env[k] = int(v.strip())
            self.parsing_data = True
        else:
            self.file.env[k] = v.strip()

    def try_close(self):
        self.nconn -= 1
        if self.nconn == 0:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()


class RemoteSublEventListener(sublime_plugin.EventListener):
    def on_post_save_async(self, view):
        env = view.settings().get('remotesubl', {})
        if env:
            display_name = env['display-name']
            try:
                if view.id() not in FILES:
                    raise

                file = FILES[view.id()]
                file.save()
                say('Saved ' + display_name)

                file_name = os.path.basename(display_name.split(':')[-1])
                if ":" in display_name:
                    server_name = os.path.basename(display_name.split(':')[0])
                else:
                    server_name = "remote server"
                sublime.set_timeout(
                    lambda: sublime.status_message("Saved {} to {}.".format(
                        file_name, server_name)))
            except:
                say('Error saving {}.'.format(display_name))
                sublime.set_timeout(
                    lambda: sublime.status_message("Error saving {}.".format(display_name)))

    def on_close(self, view):
        env = view.settings().get('remotesubl', {})
        if env:
            display_name = env['display-name']
            if view.id() in FILES:
                file = FILES.pop(view.id())
            try:
                file.close()
                say('Closed ' + display_name)
            except:
                say('Error closing {}.'.format(display_name))

    def on_activated(self, view):
        view.run_command("remote_subl_update_status_bar")


class ConnectionHandler(socketserver.BaseRequestHandler):
    def handle(self):
        say('New connection from ' + str(self.client_address))

        session = Session(self.request)
        self.request.send(b"Sublime Text 3 (remotesubl plugin)\n")

        socket_fd = self.request.makefile("rb")
        while True:
            line = socket_fd.readline()
            if(len(line) == 0):
                break
            session.parse_input(line)

        say('Connection close.')


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def unload_handler():
    global server
    say('Killing server...')
    if server:
        server.shutdown()
        server.server_close()


def plugin_loaded():
    global server

    # Load settings
    settings = sublime.load_settings("remotesubl.sublime-settings")
    port = settings.get("port", 52698)
    host = settings.get("host", "localhost")

    # Start server thread
    server = TCPServer((host, port), ConnectionHandler)
    Thread(target=server.serve_forever, args=[]).start()
    say('Server running on {}:{} ...'.format(host, str(port)))


class RemoteSublUpdateStatusBarCommand(sublime_plugin.TextCommand):

    def run(self, edit):

        env = self.view.settings().get('remotesubl', {})
        if env:
            display_name = env['display-name']
            if ":" in display_name:
                server_name = os.path.basename(display_name.split(':')[0])
            else:
                server_name = "remote"
            self.view.set_status("remotesub_status", "[{}]".format(server_name))
        else:
            self.view.erase_status("remotesub_status")
