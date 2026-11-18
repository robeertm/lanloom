# Add-on image: the released Lanloom image (build.yaml) plus the start script. The script starts as root only to
# prepare the add-on volume, then drops to the unprivileged user of the image before Lanloom starts.
ARG BUILD_FROM
FROM ${BUILD_FROM}

USER 0:0
COPY run.py /addon/run.py
RUN chmod 0755 /addon /addon/run.py

ENV LANLOOM_ADDON=1
CMD ["python", "/addon/run.py"]
