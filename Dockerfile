FROM archlinux:base-devel

ARG USER_NAME=icflow
ARG USER_ID=1000
ARG GROUP_ID=1000

RUN pacman -Syu --noconfirm --needed \
        cmake \
        curl \
        file \
        git \
        jq \
        ninja \
        python \
        rsync \
        sudo \
        vim \
    && pacman -Scc --noconfirm

RUN groupadd --gid "$GROUP_ID" "$USER_NAME" \
    && useradd --uid "$USER_ID" --gid "$GROUP_ID" --create-home --shell /bin/bash "$USER_NAME" \
    && printf '%s ALL=(root) NOPASSWD: /usr/bin/pacman\n' "$USER_NAME" \
        > "/etc/sudoers.d/$USER_NAME-pacman" \
    && chmod 0440 "/etc/sudoers.d/$USER_NAME-pacman"

COPY . /workspace/icflow_dynamic_collection
WORKDIR /workspace/icflow_dynamic_collection
RUN python -m unittest discover -s /workspace/icflow_dynamic_collection/llm_test_generation/tests -v \
    && bash -n /workspace/icflow_dynamic_collection/verify_llm_pipeline.sh \
    && bash -n /workspace/icflow_dynamic_collection/collect_dynamic.sh
RUN mkdir -p /data \
    && chown "$USER_ID:$GROUP_ID" /data \
    && chown -R "$USER_ID:$GROUP_ID" /workspace/icflow_dynamic_collection

ENV HOME=/home/${USER_NAME}
USER ${USER_NAME}

CMD ["/bin/bash"]
