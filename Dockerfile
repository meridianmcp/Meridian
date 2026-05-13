FROM ghcr.io/prefix-dev/pixi:latest
WORKDIR /app
COPY pixi.toml pixi.lock* ./
RUN pixi install
COPY . .
EXPOSE 7878
CMD ["pixi", "run", "start"]
