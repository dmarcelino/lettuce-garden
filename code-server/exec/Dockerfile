# The shell container's image (docs/code-server.md): the app-server's own image, so
# terminals and git get the agents' toolchain, plus an SSH server that only the
# editor reaches (key login, the internal code-exec network). `base` is the
# app-server's image: compose/code-server.yml passes service:app-server, so this
# follows upstream's image and tag without restating them.
# Depends on the app-server image being Debian with apt, bash and
# /usr/local/bin/agent-env (CLAUDE.md, "Named today").
FROM base
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
  && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
  && mkdir -p /run/sshd
COPY sshd_config /opt/lettuce-exec/sshd_config
WORKDIR /work
EXPOSE 22
ENTRYPOINT []
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/opt/lettuce-exec/sshd_config"]
