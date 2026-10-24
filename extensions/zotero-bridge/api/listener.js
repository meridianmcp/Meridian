'use strict';

// The only file in this plugin that opens a socket or starts a timer.
// It binds the loopback interface and reads one request per connection. When the header block is in, it
// calls options.admitHead(headers) before it reads the body, so a refused peer cannot make the listener
// buffer a body. A refusal is written at once. An admitted request has its body read and goes to
// options.handle. After any answer the socket drains input until the client hangs up or the linger timer
// fires, then it closes. The listener makes no access decisions: lib/api/auth.js and lib/api/app.js do that.
//
// Loaded by bootstrap.js into a sandbox that provides Components, Services and
// `options` (see startListener). It has no other dependencies.

var LOOPBACK_HOST = '127.0.0.1';
var MAX_CONNECTIONS = 8;
var IDLE_TIMEOUT_MS = 10000;
// After the answer is written, the socket stays open until the client hangs up or this time passes.
// Closing at once can drop bytes the socket has not sent yet, and the client then sees no response.
var LINGER_MS = 5000;

function startListener(options) {
  var Cc = Components.classes;
  var Ci = Components.interfaces;
  var server = Cc['@mozilla.org/network/server-socket;1'].createInstance(Ci.nsIServerSocket);
  var open = new Set();

  function oneShot(ms, fn) {
    var timer = Cc['@mozilla.org/timer;1'].createInstance(Ci.nsITimer);
    timer.initWithCallback({ notify: fn }, ms, Ci.nsITimer.TYPE_ONE_SHOT);
    return timer;
  }

  function cancel(timer) {
    if (!timer) return;
    try {
      timer.cancel();
    } catch (ignored) {
      // The timer already fired.
    }
  }

  function closeTransport(transport) {
    try {
      transport.close(0);
    } catch (ignored) {
      // Already closed by the other side.
    }
    open.delete(transport);
  }

  function serve(transport) {
    open.add(transport);
    var parser = options.createParser();
    var finished = false;
    var responded = false;
    var input = null;
    var output = null;
    var idle = null;
    var linger = null;
    var inputBinary = null;
    var thread = Services.tm.currentThread;

    function close() {
      if (finished) return;
      finished = true;
      cancel(idle);
      cancel(linger);
      [input, output].forEach(function (stream) {
        try {
          if (stream) stream.close();
        } catch (ignored) {
          // Already closed.
        }
      });
      closeTransport(transport);
    }

    function reply(response) {
      if (finished || responded) return;
      responded = true;
      cancel(idle);
      try {
        var outputBinary = Cc['@mozilla.org/binaryoutputstream;1'].createInstance(Ci.nsIBinaryOutputStream);
        outputBinary.setOutputStream(output);
        outputBinary.writeByteArray(Array.from(response.bytes), response.bytes.length);
        // Keep reading, so an end-of-stream from the client is seen and the socket closes then.
        input.asyncWait(callback, 0, 0, thread);
      } catch (error) {
        options.log(error);
        close();
        return;
      }
      linger = oneShot(LINGER_MS, close);
    }

    function handle(request) {
      cancel(idle);
      options.handle(request).then(reply, function (error) {
        options.log(error);
        reply(options.internalFailure());
      });
    }

    // Asks the app whether to admit a request before its body is read. Returns a response to send, or null.
    // An app that supplies no admitHead admits every request.
    function admit(headers) {
      if (typeof options.admitHead !== 'function') return null;
      try {
        return options.admitHead(headers) || null;
      } catch (error) {
        // An error in the check is answered, never admitted.
        options.log(error);
        return options.internalFailure();
      }
    }

    // Acts on the parser's latest state. Returns true once the request is answered or handed to the app.
    function advance(state) {
      if (state.status === 'error') {
        reply(options.rejectParse(state));
        return true;
      }
      if (state.status === 'head') {
        var refusal = admit(state.headers);
        if (refusal) {
          reply(refusal);
          return true;
        }
        // Admitted. The body may already be complete, so ask the parser before waiting for more input.
        return advance(parser.push(new Uint8Array(0)));
      }
      if (state.status === 'complete') {
        handle(state);
        return true;
      }
      return false;
    }

    var callback = {
      onInputStreamReady: function (stream) {
        if (finished) return;
        try {
          var available = stream.available();
          if (responded) {
            // The answer is out. Input now only means the client is leaving: end of stream closes the socket.
            if (available === 0) {
              close();
              return;
            }
            inputBinary.readByteArray(available);
            stream.asyncWait(callback, 0, 0, thread);
            return;
          }
          if (available > 0) {
            var chunk = Uint8Array.from(inputBinary.readByteArray(available));
            if (advance(parser.push(chunk))) return;
          }
          stream.asyncWait(callback, 0, 0, thread);
        } catch (error) {
          // The peer closed, or the read failed. Nothing to answer.
          close();
        }
      },
    };

    try {
      input = transport.openInputStream(0, 0, 0).QueryInterface(Ci.nsIAsyncInputStream);
      output = transport.openOutputStream(Ci.nsITransport.OPEN_BLOCKING, 0, 0);
      inputBinary = Cc['@mozilla.org/binaryinputstream;1'].createInstance(Ci.nsIBinaryInputStream);
      inputBinary.setInputStream(input);
      idle = oneShot(IDLE_TIMEOUT_MS, close);
      input.asyncWait(callback, 0, 0, thread);
    } catch (error) {
      options.log(error);
      close();
    }
  }

  // The second argument true restricts the socket to the loopback interface.
  server.init(options.port, true, -1);
  server.asyncListen({
    onSocketAccepted: function (socket, transport) {
      if (open.size >= MAX_CONNECTIONS) {
        closeTransport(transport);
        return;
      }
      serve(transport);
    },
    onStopListening: function () {},
  });

  return {
    host: LOOPBACK_HOST,
    port: server.port,
    stop: function () {
      try {
        server.close();
      } catch (ignored) {
        // Already closed.
      }
      Array.from(open).forEach(closeTransport);
    },
  };
}

module.exports = { startListener: startListener, LOOPBACK_HOST: LOOPBACK_HOST };
