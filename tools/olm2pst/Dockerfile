# Command-line converter in a container (no desktop window).
#   docker build -t olm2pst tools/olm2pst
#   docker run --rm -v "%cd%:/data" olm2pst /data/Archive.olm            (Windows cmd)
#   docker run --rm -v "${PWD}:/data" olm2pst /data/Archive.olm --list   (PowerShell)
FROM python:3.12-slim
WORKDIR /app
COPY olm2pst ./olm2pst
RUN useradd --create-home converter
USER converter
ENTRYPOINT ["python", "-m", "olm2pst"]
CMD ["--help"]
