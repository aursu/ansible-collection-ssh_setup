ARG rocky=10.1.20251126
FROM ghcr.io/aursu/rockylinux:${rocky}-ansible

COPY --chown=root:root --chmod=0755 libexec/publish /usr/local/libexec/publish

WORKDIR /var/ansible/project

CMD ["/usr/local/libexec/publish"]
