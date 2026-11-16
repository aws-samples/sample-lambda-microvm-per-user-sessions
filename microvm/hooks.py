# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
MicroVM lifecycle hooks on port 9000 (standard library only).

/ready is the important one: the platform snapshots the MicroVM's memory and
disk after it returns 200. Waiting until VS Code actually serves its workbench
means every user's MicroVM resumes from a snapshot with VS Code already up.
"""
import http.server
import urllib.request

PREFIX = "/aws/lambda-microvms/runtime/v1"
VSCODE = "http://127.0.0.1:8080/ide/"


def vscode_is_up():
    try:
        # Fixed loopback URL (VSCODE constant); never user input. Plain HTTP is fine:
        # the readiness check never leaves the MicroVM.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected, python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen
        with urllib.request.urlopen(VSCODE, timeout=2) as resp:  # nosec B310
            return resp.status == 200
    except OSError:
        return False


class Hooks(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == f"{PREFIX}/ready":
            # 503 tells the platform to ask again later.
            self.send_response(200 if vscode_is_up() else 503)
        elif self.path.startswith(PREFIX + "/"):
            # run, resume, suspend, terminate, validate: nothing to do here.
            self.send_response(200)
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # keep hook calls out of the logs
        pass


if __name__ == "__main__":
    # Lambda MicroVMs calls hooks over the guest network namespace, so they must
    # bind 0.0.0.0. Port 9000 is not reachable from outside without a MicroVM token.
    http.server.ThreadingHTTPServer(("0.0.0.0", 9000), Hooks).serve_forever()  # nosec B104
