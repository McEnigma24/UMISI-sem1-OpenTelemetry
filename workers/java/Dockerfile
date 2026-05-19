# Obraz runner: Maven (shade) → jeden fat JAR w kontekście ./workers/java.
#
#   docker compose build worker-java
#   cd workers/java && ../compose z repo: ./docker_build.sh
#
FROM maven:3.9.9-eclipse-temurin-21 AS builder
WORKDIR /build
COPY pom.xml .
COPY src ./src
RUN mvn -q -DskipTests package

FROM eclipse-temurin:21-jre-jammy
WORKDIR /app
COPY --from=builder /build/target/worker-java.jar /app/app.jar
CMD ["/bin/bash", "-c", "sleep 2 && exec java -jar /app/app.jar"]
