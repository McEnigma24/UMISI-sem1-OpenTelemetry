FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

COPY OtelDemo.csproj .
RUN dotnet restore OtelDemo.csproj

COPY . .
RUN dotnet publish OtelDemo.csproj -c Release -o /app/publish

FROM mcr.microsoft.com/dotnet/runtime:8.0
WORKDIR /app
COPY --from=build /app/publish .


CMD ["/bin/sh", "-c", "sleep 2 && exec dotnet client_csharp.dll"]
