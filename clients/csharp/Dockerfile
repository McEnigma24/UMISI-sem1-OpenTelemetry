FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

COPY OtelDemo.csproj .
RUN dotnet restore OtelDemo.csproj

COPY . .
RUN dotnet publish OtelDemo.csproj -c Release -o /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:8.0

# Pyroscope .NET profiler — biblioteki z oficjalnego obrazu (Linux x86_64).
# Na hoście ARM (Apple Silicon) serwis `client-csharp` w compose ma `platform: linux/amd64`.
COPY --from=pyroscope/pyroscope-dotnet:0.14.2-glibc /Pyroscope.Profiler.Native.so /dotnet/Pyroscope.Profiler.Native.so
COPY --from=pyroscope/pyroscope-dotnet:0.14.2-glibc /Pyroscope.Linux.ApiWrapper.x64.so /dotnet/Pyroscope.Linux.ApiWrapper.x64.so

# Logi natywnego profilera (fork DD) — compose ustawia DD_TRACE_LOG_DIRECTORY; katalog musi istnieć.
RUN mkdir -p /var/log/pyroscope/dotnet && chmod a+rwx /var/log/pyroscope/dotnet

WORKDIR /app
COPY --from=build /app/publish .

ENV ASPNETCORE_URLS=http://0.0.0.0:8080
ENV LD_LIBRARY_PATH=/dotnet

CMD ["/bin/sh", "-c", "sleep 2 && exec dotnet client_csharp.dll"]
