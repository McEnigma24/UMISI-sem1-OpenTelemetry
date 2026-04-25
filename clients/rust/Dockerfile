FROM rust:slim-trixie

WORKDIR /app

COPY . .

RUN rustup component add rustfmt

RUN chmod +x ./start.sh



CMD ["cargo", "fmt"]
CMD ["cargo", "run"]

# CMD ["./start.sh"]