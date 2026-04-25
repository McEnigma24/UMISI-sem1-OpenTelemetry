FROM rust:slim-trixie

WORKDIR /app

COPY . .

RUN rustup component add rustfmt

RUN chmod +x ./start.sh


CMD ["/bin/sh", "-c", "sleep 2 && cargo fmt && exec cargo run"]

# CMD ["cargo", "fmt"]
# CMD ["cargo", "run"]

# CMD ["./start.sh"]