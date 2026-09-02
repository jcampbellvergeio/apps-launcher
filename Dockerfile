# A try-it image, not a deployment.
#
# The launcher's job is to start and watch processes on YOUR machine. A
# container has its own PID, mount and network namespaces, so a containerised
# launcher can only see and start processes inside the container -- it cannot
# manage the apps on your host, and it cannot install a login item there
# either. What it CAN do is show you the whole thing working, with two demo
# apps to start, stop, embed and read logs from:
#
#   docker run --rm -p 5058:5058 apps-launcher
#
# Then open http://127.0.0.1:5058/. To run it for real, install it on the host
# (see the README) rather than here.

FROM python:3.12-slim

# curl is only here so the image can be health-checked from inside.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git \
 && rm -rf /var/lib/apt/lists/*

# psutil is the engine's fast path; the demo has it so the container behaves
# like a well-set-up host.
RUN pip install --no-cache-dir "flask>=3.0" psutil

WORKDIR /app/launcher
COPY . /app/launcher

# `dir` in the registry resolves against the PARENT of launcher/, which is the
# layout the launcher expects: a projects folder with launcher/ inside it.
RUN cp -r /app/launcher/demo/HelloApi /app/HelloApi \
 && cp -r /app/launcher/demo/Clock    /app/Clock \
 && cp /app/launcher/docker/apps.json /app/launcher/apps.json

# The UI binds loopback by default, which inside a container is unreachable
# from outside it -- `-p 5058:5058` forwards to the container's own interface.
# Safe here because the container boundary is the fence.
ENV LAUNCHER_HOST=0.0.0.0

EXPOSE 5058 5061 5062

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -sf http://127.0.0.1:5058/api/status > /dev/null || exit 1

# Start the demo apps, then run the UI in the foreground as the container's
# main process. The UI is registered with autostart:false precisely so this
# doesn't start a second copy of it.
CMD ["sh", "-c", "python devapps.py start; exec python app.py"]
