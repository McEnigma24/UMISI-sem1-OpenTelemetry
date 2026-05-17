FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

COPY OtelDemo.csproj .
RUN dotnet restore OtelDemo.csproj

COPY . .
RUN dotnet publish OtelDemo.csproj -c Release -o /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/publish .

ENV ASPNETCORE_URLS=http://0.0.0.0:8080

CMD ["/bin/sh", "-c", "sleep 2 && exec dotnet client_csharp.dll"]
