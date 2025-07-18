import http.server
import socketserver

# Define the port the server will listen on
PORT = 8888

class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    A simple HTTP request handler that responds to POST requests.
    """
    def do_POST(self):
        """
        Handles POST requests.
        Responds with a 201 Created status code and no body.
        """
        print(f"Received POST request on path: {self.path}")
        # Send the response status code
        self.send_response(201)
        # End the headers
        self.end_headers()
        # No body is sent back for a 201 response in this simple case.

    def do_GET(self):
        """
        Handles GET requests with a simple message.
        This helps to confirm the server is running via a browser or curl.
        """
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><head><title>Server Active</title></head>")
        self.wfile.write(b"<body><p>Server is running. Make a POST request to this endpoint.</p></body></html>")


def run_server():
    """
    Starts the HTTP server.
    """
    # Using TCPServer to create a server instance
    with socketserver.TCPServer(("", PORT), SimpleHTTPRequestHandler) as httpd:
        print(f"Serving at port {PORT}")
        print(f"You can access the server at http://localhost:{PORT}")
        try:
            # Keep the server running until it's interrupted
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping server...")
            httpd.server_close()

if __name__ == "__main__":
    run_server()

