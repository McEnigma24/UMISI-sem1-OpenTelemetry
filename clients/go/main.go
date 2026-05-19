// HTTP pipeline worker (Go) — W3C propagate, forward, processing_steps + nested_route (jak Python/Rust).
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/grafana/pyroscope-go"
	"github.com/shirou/gopsutil/v4/process"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	otlog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/metric"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/propagation"
)

// --- JSON (format jak traffic generator / Python) ---

// PreferGo: stabilniejsze zapytania do 127.0.0.11 (embedded DNS) niż domyślny libc na części obrazów/WSL.
var httpClient = &http.Client{
	Timeout: 120 * time.Second,
	Transport: &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   30 * time.Second,
			KeepAlive: 30 * time.Second,
			Resolver: &net.Resolver{
				PreferGo: true,
			},
		}).DialContext,
		ForceAttemptHTTP2:     false,
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	},
}

type processingStep struct {
	Activity    string         `json:"activity"`
	Time        string         `json:"time"`
	NestedRoute []routeSegment `json:"nested_route,omitempty"`
}

type routeSegment struct {
	ID               string           `json:"id"`
	Visited          bool             `json:"visited"`
	ProcessingTime   *json.RawMessage `json:"processing_time,omitempty"`
	ProcessingSteps  []processingStep `json:"processing_steps"`
}

type pipelineMsg struct {
	Route            []routeSegment `json:"route"`
	VisitLog         []string       `json:"visit_log"`
	Counter          string         `json:"counter"`
	TableOfClients   []string       `json:"table_of_clients"`
}

// --- Parsed steps ---

type parsedCPU struct {
	activity string
	sec      float64
	index    int
}

type parsedNested struct {
	activity    string
	preSec      float64
	nestedRoute []routeSegment
	index       int
}

type appState struct {
	clientID     string
	httpPath     string
	peers        map[string]string
	maxProcSec   float64
	tracer       trace.Tracer
	hopHist      metric.Float64Histogram
	msgCounter   metric.Int64Counter
	useOtlpLog   bool
	serviceName  string
}

func envOr(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}

func useOtlp() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("OTEL_DEMO_TRACE_EXPORT"))) {
	case "", "otlp", "http":
		return true
	case "ostream":
		return false
	default:
		return true
	}
}

func useOtlpLogExport() bool {
	if !useOtlp() {
		return false
	}
	v := strings.ToLower(strings.TrimSpace(envOr("OTEL_DEMO_LOG_EXPORT", "otlp")))
	switch v {
	case "0", "false", "no", "off":
		return false
	default:
		return true
	}
}

func processMetricsEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("DEMO_PROCESS_METRICS")))
	switch v {
	case "0", "false", "no", "off":
		return false
	default:
		return true
	}
}

func buildResource() *resource.Resource {
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "unknown"
	}
	iid := strings.TrimSpace(os.Getenv("OTEL_SERVICE_INSTANCE_ID"))
	if iid == "" {
		iid = uuid.New().String()
	}
	envName := strings.TrimSpace(os.Getenv("OTEL_ENVIRONMENT"))
	if envName == "" {
		envName = strings.TrimSpace(os.Getenv("DEPLOYMENT_ENVIRONMENT"))
	}
	if envName == "" {
		envName = "local"
	}
	svc := strings.TrimSpace(os.Getenv("OTEL_SERVICE_NAME"))
	if svc == "" {
		svc = "worker_go"
	}
	attrs := []attribute.KeyValue{
		semconv.ServiceName(svc),
		semconv.ServiceVersion("1.0.0"),
		attribute.String("service.instance.id", iid),
		attribute.String("deployment.environment", envName),
		attribute.String("host.name", hostname),
		attribute.String("telemetry.sdk.language", "go"),
		attribute.String("telemetry.sdk.name", "opentelemetry"),
	}
	if tag := strings.TrimSpace(os.Getenv("OTEL_DEMO_RESOURCE_TAG")); tag != "" {
		attrs = append(attrs, attribute.String("demo.instance.tag", tag))
	}
	r, _ := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(semconv.SchemaURL, attrs...),
	)
	return r
}

func otlpTracesEndpoint() string {
	return strings.TrimSpace(envOr("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"))
}

func otlpMetricsEndpoint() string {
	if e := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")); e != "" {
		return e
	}
	return strings.Replace(otlpTracesEndpoint(), "/v1/traces", "/v1/metrics", 1)
}

func otlpLogsEndpoint() string {
	if e := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")); e != "" {
		return e
	}
	return strings.Replace(otlpTracesEndpoint(), "/v1/traces", "/v1/logs", 1)
}

func processMetricAttrs() []attribute.KeyValue {
	sn := strings.TrimSpace(os.Getenv("OTEL_SERVICE_NAME"))
	if sn == "" {
		sn = "worker_go"
	}
	cid := strings.TrimSpace(os.Getenv("DEMO_CLIENT_ID"))
	if cid == "" {
		cid = "go"
	}
	return []attribute.KeyValue{
		attribute.String("service.name", sn),
		attribute.String("demo.client_id", cid),
	}
}

func pipelineMetricAttrs(clientID string) []attribute.KeyValue {
	a := processMetricAttrs()
	return append(append([]attribute.KeyValue{}, a...), attribute.String("client_id", clientID))
}

func maxProcessingSec() float64 {
	s := strings.TrimSpace(os.Getenv("DEMO_MAX_PROCESSING_SEC"))
	if s == "" {
		return 120
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || f < 0 {
		return 120
	}
	return f
}

func loadPeerMap() (map[string]string, error) {
	raw := strings.TrimSpace(os.Getenv("DEMO_PEER_MAP"))
	if raw != "" {
		var m map[string]string
		if err := json.Unmarshal([]byte(raw), &m); err != nil {
			return nil, err
		}
		out := make(map[string]string)
		for k, v := range m {
			kk := strings.ToLower(strings.TrimSpace(k))
			vv := strings.TrimSpace(v)
			if kk != "" && vv != "" {
				out[kk] = vv
			}
		}
		return out, nil
	}
	out := make(map[string]string)
	for _, e := range os.Environ() {
		k, v, ok := strings.Cut(e, "=")
		if !ok {
			continue
		}
		if k == "DEMO_PEER_MAP" {
			continue
		}
		if rest, ok := strings.CutPrefix(k, "DEMO_PEER_"); ok {
			tail := strings.ToLower(strings.TrimSpace(rest))
			vv := strings.TrimSpace(v)
			if tail != "" && vv != "" {
				out[tail] = vv
			}
		}
	}
	return out, nil
}

// materializePeerHosts zamienia nazwy serwisów Dockera (np. client-nodejs) na adres IP z jednorazowego
// LookupHost przy starcie — unika powtarzalnego DNS 127.0.0.11 na każdym forwardzie (WSL/UDP timeout).
// Po zmianie IP peera (redeploy) zrestartuj tego workera.
func materializePeerHosts(peers map[string]string) map[string]string {
	r := &net.Resolver{PreferGo: true}
	out := make(map[string]string, len(peers))
	for k, raw := range peers {
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" {
			out[k] = raw
			continue
		}
		host := u.Hostname()
		port := u.Port()
		if port == "" {
			if u.Scheme == "https" {
				port = "443"
			} else {
				port = "80"
			}
		}
		if host == "" || net.ParseIP(host) != nil {
			out[k] = raw
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		addrs, err := r.LookupHost(ctx, host)
		cancel()
		if err != nil || len(addrs) == 0 {
			log.Printf("peer materialize: lookup %q (%s): %v — zostaje hostname w URL", host, k, err)
			out[k] = raw
			continue
		}
		ip := pickDialIP(addrs)
		if ip == "" {
			out[k] = raw
			continue
		}
		u2 := *u
		u2.Host = net.JoinHostPort(ip, port)
		fixed := u2.String()
		if fixed != raw {
			log.Printf("peer %s: %q → %q (DNS tylko przy starcie)", k, raw, fixed)
		}
		out[k] = fixed
	}
	return out
}

func pickDialIP(addrs []string) string {
	for _, a := range addrs {
		ip := net.ParseIP(a)
		if ip != nil && ip.To4() != nil {
			return ip.String()
		}
	}
	if len(addrs) > 0 {
		return addrs[0]
	}
	return ""
}

func parseProcessingTime(s string, capSec float64) (float64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	low := strings.ToLower(s)
	var sec float64
	var err error
	if strings.HasSuffix(low, "ms") {
		n := strings.TrimSpace(strings.TrimSuffix(low, "ms"))
		sec, err = strconv.ParseFloat(n, 64)
		if err != nil {
			return 0, fmt.Errorf("invalid processing_time: %q", s)
		}
		sec /= 1000
	} else if strings.HasSuffix(low, "s") {
		n := strings.TrimSpace(strings.TrimSuffix(low, "s"))
		sec, err = strconv.ParseFloat(n, 64)
		if err != nil {
			return 0, fmt.Errorf("invalid processing_time: %q", s)
		}
	} else {
		return 0, fmt.Errorf("invalid processing_time: %q (use e.g. 5.6s or 100ms)", s)
	}
	if sec < 0 {
		return 0, fmt.Errorf("processing_time must be non-negative")
	}
	if sec > capSec {
		sec = capSec
	}
	return sec, nil
}

func segmentSchemaErr(seg *routeSegment, where string) string {
	if seg.ProcessingTime != nil {
		return where + ": field 'processing_time' is not supported; use non-empty 'processing_steps' with {\"activity\",\"time\"}"
	}
	if len(seg.ProcessingSteps) == 0 {
		return where + ": non-empty 'processing_steps' is required"
	}
	return ""
}

func validateNestedRoute(raw []routeSegment, stepI int) ([]routeSegment, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("processing_steps[%d].nested_route must be a non-empty array", stepI)
	}
	for j := range raw {
		sch := segmentSchemaErr(&raw[j], fmt.Sprintf("processing_steps[%d].nested_route[%d]", stepI, j))
		if sch != "" {
			return nil, fmt.Errorf("%s", sch)
		}
		if strings.TrimSpace(raw[j].ID) == "" {
			return nil, fmt.Errorf("processing_steps[%d].nested_route[%d].id must be a non-empty string", stepI, j)
		}
	}
	first := strings.ToLower(strings.TrimSpace(raw[0].ID))
	if first == "py" {
		return nil, fmt.Errorf("processing_steps[%d].nested_route[0].id must not be 'py' (workers only)", stepI)
	}
	return raw, nil
}

func parseProcessingSteps(seg *routeSegment, cap float64) ([]any, error) {
	if len(seg.ProcessingSteps) == 0 {
		return nil, nil
	}
	var out []any
	for i, st := range seg.ProcessingSteps {
		act := strings.TrimSpace(st.Activity)
		if act == "" {
			act = fmt.Sprintf("step_%d", i)
		}
		if len(st.NestedRoute) > 0 {
			nr, err := validateNestedRoute(st.NestedRoute, i)
			if err != nil {
				return nil, err
			}
			pre, err := parseProcessingTime(st.Time, cap)
			if err != nil {
				return nil, fmt.Errorf("processing_steps[%d].time: %w", i, err)
			}
			out = append(out, parsedNested{activity: act, preSec: pre, nestedRoute: nr, index: i})
		} else {
			sec, err := parseProcessingTime(st.Time, cap)
			if err != nil {
				return nil, fmt.Errorf("processing_steps[%d].time: %w", i, err)
			}
			out = append(out, parsedCPU{activity: act, sec: sec, index: i})
		}
	}
	return out, nil
}

func spanNameForActivity(act string) string {
	t := strings.TrimSpace(act)
	if t == "" {
		return "pipeline.processing_step"
	}
	r := []rune(t)
	if len(r) > 256 {
		return string(r[:256])
	}
	return t
}

func firstUnvisited(route []routeSegment) int {
	for i := range route {
		if !route[i].Visited {
			return i
		}
	}
	return -1
}

func peerURL(peers map[string]string, segID string) string {
	nid := strings.ToLower(strings.TrimSpace(segID))
	if u, ok := peers[nid]; ok {
		return u
	}
	if u, ok := peers[strings.TrimSpace(segID)]; ok {
		return u
	}
	return ""
}

func bumpCounterTable(m *pipelineMsg, clientID string) {
	c, _ := strconv.Atoi(strings.TrimSpace(m.Counter))
	m.Counter = strconv.Itoa(c + 1)
	m.TableOfClients = append(m.TableOfClients, clientID)
}

func nestedPipelineMsg(nr []routeSegment) pipelineMsg {
	cp := make([]routeSegment, len(nr))
	copy(cp, nr)
	for i := range cp {
		cp[i].Visited = false
	}
	return pipelineMsg{
		Route:          cp,
		VisitLog:       []string{},
		Counter:        "0",
		TableOfClients: []string{},
	}
}

func cpuSpin(ctx context.Context, sec float64) {
	if sec <= 0 {
		return
	}
	deadline := time.Now().Add(time.Duration(sec * float64(time.Second)))
	var v uint64 = 1
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return
		default:
		}
		for i := 0; i < 2048; i++ {
			v = v*1103515245 + 12345
			v &= 0x7fffffff
		}
		runtime.Gosched()
	}
}

func (st *appState) goLine(msg string) {
	ts := time.Now().Format("15:04:05.000")
	line := fmt.Sprintf("%s %s", ts, msg)
	fmt.Println(line)
	if !st.useOtlpLog {
		return
	}
	lp := global.GetLoggerProvider()
	if lp == nil {
		return
	}
	var rec otlog.Record
	rec.SetBody(otlog.StringValue(line))
	rec.SetSeverity(otlog.SeverityInfo)
	lp.Logger("demo.pipeline").Emit(context.Background(), rec)
}

func maybePyroscope() (stop func(), err error) {
	stop = func() {}
	en := strings.ToLower(strings.TrimSpace(envOr("PYROSCOPE_ENABLED", "true")))
	switch en {
	case "0", "false", "no", "off":
		return stop, nil
	}
	srv := strings.TrimSpace(os.Getenv("PYROSCOPE_SERVER"))
	if srv == "" {
		return stop, nil
	}
	app := strings.TrimSpace(envOr("OTEL_SERVICE_NAME", "worker_go"))
	cid := strings.TrimSpace(envOr("DEMO_CLIENT_ID", "go"))
	py, err := pyroscope.Start(pyroscope.Config{
		ApplicationName: app,
		ServerAddress:   srv,
		Tags: map[string]string{
			// Pyroscope tag keys: bez '.' (validator godeltaprof / serwera).
			"service_name":    app,
			"demo_client_id": cid,
		},
	})
	if err != nil {
		return stop, err
	}
	fmt.Printf("%s Pyroscope push profiler: server='%s' application_name='%s'\n",
		time.Now().Format("15:04:05.000"), srv, app)
	return func() { _ = py.Stop() }, nil
}

func transientPipelineHTTPDialErr(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	// Nie powtarzaj błędów DNS — kilka prób × timeout resolvera daje ~50s w Jaegerze.
	return strings.Contains(s, "connection refused") ||
		strings.Contains(s, "connection reset by peer") ||
		strings.Contains(s, "no route to host")
}

func httpPostJSONAttempt(ctx context.Context, url string, body []byte, hdr http.Header) (int, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	for k, vals := range hdr {
		for _, v := range vals {
			req.Header.Add(k, v)
		}
	}
	if req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	return resp.StatusCode, b, err
}

func httpPostJSON(ctx context.Context, url string, body []byte, hdr http.Header) (int, []byte, error) {
	const maxAttempts = 3
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(80+attempt*120) * time.Millisecond
			t := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				t.Stop()
				return 0, nil, ctx.Err()
			case <-t.C:
			}
			t.Stop()
		}
		code, b, err := httpPostJSONAttempt(ctx, url, body, hdr)
		if err == nil {
			return code, b, nil
		}
		lastErr = err
		if !transientPipelineHTTPDialErr(err) {
			return 0, nil, err
		}
	}
	return 0, nil, lastErr
}

func (st *appState) handlePipeline(w http.ResponseWriter, r *http.Request) {
	t0 := time.Now()
	path := st.httpPath
	if r.URL.Path != path && strings.TrimSuffix(r.URL.Path, "/") != strings.TrimSuffix(path, "/") {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "read body", http.StatusBadRequest)
		return
	}
	defer func() {
		ms := float64(time.Since(t0).Milliseconds())
		st.hopHist.Record(r.Context(), ms, metric.WithAttributes(pipelineMetricAttrs(st.clientID)...))
	}()

	var msg pipelineMsg
	if err := json.Unmarshal(body, &msg); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	st.goLine(fmt.Sprintf("[%s] received: %s", st.clientID, string(body)))

	prop := otel.GetTextMapPropagator()
	parentCtx := prop.Extract(r.Context(), propagation.HeaderCarrier(r.Header))

	tr := st.tracer
	ctx, hopSpan := tr.Start(parentCtx, "pipeline.hop",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(attribute.String("demo.client_id", st.clientID)),
	)
	defer hopSpan.End()

	if len(msg.Route) == 0 {
		hopSpan.SetStatus(codes.Error, "empty route")
		http.Error(w, "JSON must include non-empty route array", http.StatusBadRequest)
		return
	}
	idx := firstUnvisited(msg.Route)
	if idx < 0 {
		hopSpan.SetStatus(codes.Error, "no unvisited")
		http.Error(w, "route has no unvisited segment", http.StatusBadRequest)
		return
	}
	seg := &msg.Route[idx]
	if strings.TrimSpace(seg.ID) != st.clientID {
		hopSpan.SetStatus(codes.Error, "id mismatch")
		http.Error(w, fmt.Sprintf("first unvisited route segment id must match this node (expected %q, got %q)", st.clientID, seg.ID), http.StatusBadRequest)
		return
	}
	if sch := segmentSchemaErr(seg, "route segment"); sch != "" {
		hopSpan.SetStatus(codes.Error, sch)
		http.Error(w, sch, http.StatusBadRequest)
		return
	}
	stepsAny, err := parseProcessingSteps(seg, st.maxProcSec)
	if err != nil {
		hopSpan.SetStatus(codes.Error, err.Error())
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if len(stepsAny) == 0 {
		hopSpan.SetStatus(codes.Error, "steps")
		http.Error(w, "route segment: non-empty 'processing_steps' is required", http.StatusBadRequest)
		return
	}

	var cpuSum float64
	hasNested := false
	for _, s := range stepsAny {
		switch v := s.(type) {
		case parsedCPU:
			cpuSum += v.sec
		case parsedNested:
			cpuSum += v.preSec
			hasNested = true
		}
	}

	ctx2, procSpan := tr.Start(ctx, "pipeline.processing",
		trace.WithAttributes(
			attribute.String("demo.processing.mode", "steps"),
			attribute.Int("demo.step_count", len(stepsAny)),
			attribute.Float64("demo.simulated_processing_sec", cpuSum),
			attribute.Bool("demo.has_nested_steps", hasNested),
		),
	)
	defer procSpan.End()

	for _, s := range stepsAny {
		switch step := s.(type) {
		case parsedCPU:
			name := spanNameForActivity(step.activity)
			stepCtx, sp := tr.Start(ctx2, name,
				trace.WithAttributes(
					attribute.String("demo.activity", step.activity),
					attribute.Float64("demo.cpu_spin_sec", step.sec),
					attribute.Int("demo.step_index", step.index),
				),
			)
			cpuSpin(stepCtx, step.sec)
			sp.End()
		case parsedNested:
			name := spanNameForActivity(step.activity)
			stepCtx, sp := tr.Start(ctx2, name,
				trace.WithAttributes(
					attribute.String("demo.activity", step.activity),
					attribute.Float64("demo.cpu_spin_sec", step.preSec),
					attribute.Int("demo.step_index", step.index),
					attribute.Bool("demo.nested_subpipeline", true),
				),
			)
			cpuSpin(stepCtx, step.preSec)
			firstID := strings.TrimSpace(step.nestedRoute[0].ID)
			nurl := peerURL(st.peers, firstID)
			if nurl == "" {
				sp.SetStatus(codes.Error, "no peer nested")
				sp.End()
				http.Error(w, fmt.Sprintf("no peer URL for nested id %q", firstID), http.StatusBadGateway)
				return
			}
			nestBody, _ := json.Marshal(nestedPipelineMsg(step.nestedRoute))
			st.goLine(fmt.Sprintf("[%s] nested_forward to %s: %s", st.clientID, nurl, string(nestBody)))

			nfCtx, nfSpan := tr.Start(stepCtx, "pipeline.nested_forward",
				trace.WithSpanKind(trace.SpanKindClient),
				trace.WithAttributes(attribute.String("http.url", strings.TrimSpace(nurl))),
			)
			h := make(http.Header)
			prop.Inject(nfCtx, propagation.HeaderCarrier(h))
			code, _, err := httpPostJSON(nfCtx, strings.TrimSpace(nurl), nestBody, h)
			if err != nil {
				nfSpan.RecordError(err)
				nfSpan.SetStatus(codes.Error, err.Error())
				nfSpan.End()
				sp.End()
				http.Error(w, "nested_forward failed: "+err.Error(), http.StatusBadGateway)
				return
			}
			nfSpan.SetAttributes(attribute.Int("demo.downstream_status", code))
			nfSpan.End()
			if code < 200 || code >= 300 {
				sp.SetStatus(codes.Error, "nested http")
				sp.End()
				http.Error(w, fmt.Sprintf("nested_forward HTTP %d", code), http.StatusBadGateway)
				return
			}
			sp.End()
		}
	}

	seg.Visited = true
	msg.VisitLog = append(msg.VisitLog, st.clientID)
	bumpCounterTable(&msg, st.clientID)

	nextIdx := firstUnvisited(msg.Route)
	outBody, err := json.Marshal(msg)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	hopSpan.SetAttributes(
		attribute.String("demo.counter", msg.Counter),
		attribute.Int("demo.table_len", len(msg.TableOfClients)),
		attribute.Bool("demo.has_forward", nextIdx >= 0),
	)

	if nextIdx < 0 {
		st.goLine(fmt.Sprintf("[%s] respond (terminal route): %s", st.clientID, string(outBody)))
		st.msgCounter.Add(r.Context(), 1, metric.WithAttributes(pipelineMetricAttrs(st.clientID)...))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(outBody)
		return
	}

	nextID := strings.TrimSpace(msg.Route[nextIdx].ID)
	nextURL := peerURL(st.peers, nextID)
	if nextURL == "" {
		http.Error(w, fmt.Sprintf("no peer URL for id %q", nextID), http.StatusBadGateway)
		return
	}
	st.goLine(fmt.Sprintf("[%s] forward to %s: %s", st.clientID, nextURL, string(outBody)))

	fwCtx, fwSpan := tr.Start(ctx2, "pipeline.forward",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("http.url", strings.TrimSpace(nextURL))),
	)
	h := make(http.Header)
	prop.Inject(fwCtx, propagation.HeaderCarrier(h))
	code, respBody, err := httpPostJSON(fwCtx, strings.TrimSpace(nextURL), outBody, h)
	if err != nil {
		fwSpan.RecordError(err)
		fwSpan.SetStatus(codes.Error, err.Error())
		fwSpan.End()
		http.Error(w, "forward failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	fwSpan.SetAttributes(attribute.Int("demo.downstream_status", code))
	fwSpan.End()

	st.msgCounter.Add(r.Context(), 1, metric.WithAttributes(pipelineMetricAttrs(st.clientID)...))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write(respBody)
}

func main() {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("DEMO_MODE")), "exercises") {
		fmt.Println("DEMO_MODE=exercises — użyj DEMO_MODE=pipeline.")
		return
	}

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	res := buildResource()
	ctx := context.Background()

	var tp *sdktrace.TracerProvider
	var mp *sdkmetric.MeterProvider
	var lp *sdklog.LoggerProvider

	if useOtlp() {
		texp, err := otlptracehttp.New(ctx,
			otlptracehttp.WithEndpointURL(otlpTracesEndpoint()),
		)
		if err != nil {
			log.Fatal(err)
		}
		// SimpleSpanProcessor: każdy span idzie na OTLP od razu po End — Jaeger nie pokazuje
		// „sierot”, gdy dziecko (np. drugi worker_go) trafia do backendu przed rodzicem z batcha.
		tp = sdktrace.NewTracerProvider(
			sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(texp)),
			sdktrace.WithResource(res),
		)
	} else {
		ex, _ := stdouttrace.New()
		tp = sdktrace.NewTracerProvider(
			sdktrace.WithSyncer(ex),
			sdktrace.WithResource(res),
		)
	}
	otel.SetTracerProvider(tp)

	if useOtlp() {
		mexp, err := otlpmetrichttp.New(ctx,
			otlpmetrichttp.WithEndpointURL(otlpMetricsEndpoint()),
		)
		if err != nil {
			log.Fatal(err)
		}
		r := sdkmetric.NewPeriodicReader(mexp, sdkmetric.WithInterval(5*time.Second))
		mp = sdkmetric.NewMeterProvider(sdkmetric.WithReader(r), sdkmetric.WithResource(res))
		otel.SetMeterProvider(mp)
	}

	useLog := useOtlpLogExport()
	if useOtlp() && useLog {
		lexp, err := otlploghttp.New(ctx,
			otlploghttp.WithEndpointURL(otlpLogsEndpoint()),
		)
		if err != nil {
			log.Printf("otlp logs: %v", err)
		} else {
			lp = sdklog.NewLoggerProvider(
				sdklog.WithProcessor(sdklog.NewBatchProcessor(lexp)),
				sdklog.WithResource(res),
			)
			global.SetLoggerProvider(lp)
		}
	}

	meter := otel.Meter(strings.TrimSpace(envOr("OTEL_SERVICE_NAME", "worker_go")), metric.WithInstrumentationVersion("1.0.0"))
	hopHist, err := meter.Float64Histogram(
		"demo.pipeline.hop.duration_ms",
		metric.WithUnit("ms"),
		metric.WithDescription("Czas przetworzenia i ewent. forward jednego hopy"),
	)
	if err != nil {
		log.Fatal(err)
	}
	msgCounter, err := meter.Int64Counter(
		"demo.pipeline.messages",
		metric.WithDescription("Liczba przetworzonych wiadomości w węźle"),
	)
	if err != nil {
		log.Fatal(err)
	}

	if useOtlp() && processMetricsEnabled() {
		proc, _ := process.NewProcess(int32(os.Getpid()))
		attrs := processMetricAttrs()
		cpuG, err := meter.Float64ObservableGauge(
			"demo.process.cpu.utilization",
			metric.WithUnit("%"),
			metric.WithDescription("Użycie CPU procesu 0–100"),
		)
		if err == nil {
			memG, err2 := meter.Int64ObservableGauge(
				"demo.process.memory.usage",
				metric.WithUnit("By"),
				metric.WithDescription("RSS procesu (bajty)"),
			)
			if err2 == nil {
				_, _ = meter.RegisterCallback(func(ctx context.Context, o metric.Observer) error {
					cpu, err := proc.Percent(0)
					if err == nil {
						v := math.Min(100, math.Max(0, cpu))
						o.ObserveFloat64(cpuG, v, metric.WithAttributes(attrs...))
					}
					mi, err := proc.MemoryInfo()
					if err == nil && mi != nil {
						o.ObserveInt64(memG, int64(mi.RSS), metric.WithAttributes(attrs...))
					}
					return nil
				}, cpuG, memG)
			}
		}
	}

	stopPy, perr := maybePyroscope()
	defer stopPy()
	if perr != nil {
		log.Printf("pyroscope: %v", perr)
	}

	peers, err := loadPeerMap()
	if err != nil {
		log.Fatalf("peer map: %v", err)
	}
	peers = materializePeerHosts(peers)

	clientID := strings.TrimSpace(envOr("DEMO_CLIENT_ID", "go"))
	httpPath := envOr("DEMO_HTTP_PATH", "/v1/pipeline")
	svcName := strings.TrimSpace(envOr("OTEL_SERVICE_NAME", "worker_go"))

	st := &appState{
		clientID:     clientID,
		httpPath:     httpPath,
		peers:        peers,
		maxProcSec:   maxProcessingSec(),
		tracer:       tp.Tracer(svcName, trace.WithInstrumentationVersion("1.0.0")),
		hopHist:      hopHist,
		msgCounter:   msgCounter,
		useOtlpLog:   useLog && lp != nil,
		serviceName:  svcName,
	}

	addr := envOr("DEMO_HTTP_ADDR", "0.0.0.0:8080")
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == httpPath || strings.TrimSuffix(r.URL.Path, "/") == strings.TrimSuffix(httpPath, "/") {
			st.handlePipeline(w, r)
			return
		}
		http.NotFound(w, r)
	})

	fmt.Printf("%s pipeline: listen http://%s%s client_id=%q peers=%v\n",
		time.Now().Format("15:04:05.000"), addr, httpPath, clientID, keysOf(peers))

	log.Fatal(http.ListenAndServe(addr, mux))
}

func keysOf(m map[string]string) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}
