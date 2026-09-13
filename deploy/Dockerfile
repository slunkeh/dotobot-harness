# Control plane only. Bot computers remain separate, hardened sibling containers.
FROM docker:28-cli AS dockercli
FROM python:3.12-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8765
# The installer mounts the state at the SAME absolute path as on the Docker
# host. Per-bot credential bind mounts therefore resolve identically in both.
# Only this controller receives the engine socket; never a bot computer.
CMD ["python", "deploy/container_entrypoint.py"]
