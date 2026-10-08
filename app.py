import dash
from dash import html, Input, Output, State
from dash.dcc.Dropdown import Dropdown
from dash.dcc.Store import Store
from dash.dcc.Graph import Graph
from dash.dcc.Interval import Interval
import plotly.graph_objs as go
import docker
import pandas as pd
from datetime import datetime, timedelta
from dash_bootstrap_components import themes
from dash_bootstrap_components._components.Container import Container
from dash_bootstrap_components._components.Row import Row
from dash_bootstrap_components._components.Col import Col
from dash_bootstrap_components._components.Label import Label
from dash_bootstrap_components._components.Button import Button
from dash_bootstrap_components._components.Table import Table
from collections import deque
import threading
import time
import re

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


# Initialize Docker client
client = docker.from_env()

# Data storage: Keep 5 minutes of data (300 samples at 1 sample/sec)
MAX_SAMPLES = 300
data_store = {
    "timestamps": deque(maxlen=MAX_SAMPLES),
    "cpu": deque(maxlen=MAX_SAMPLES),
    "mem": deque(maxlen=MAX_SAMPLES),
    "logs": deque(maxlen=MAX_SAMPLES),
    "container_name": None,
}


def get_mem_mib(stats):
    """Extract memory in MiB from docker stats."""
    mem_bytes = stats["memory_stats"].get("usage", 0)
    stats_dict = stats["memory_stats"].get("stats", {})
    cache = stats_dict.get("inactive_file", stats_dict.get("cache", 0))
    return (mem_bytes - cache) / (1024 * 1024)


def ansi_to_dash(text):
    """Convert ANSI color codes to a list of Dash html.Span components."""
    if not text:
        return []
    color_map = {
        "30": "black",
        "31": "red",
        "32": "green",
        "33": "yellow",
        "34": "blue",
        "35": "magenta",
        "36": "cyan",
        "37": "white",
        "90": "gray",
        "91": "lightred",
        "92": "lightgreen",
        "93": "lightyellow",
        "94": "lightblue",
        "95": "lightmagenta",
        "96": "lightcyan",
        "97": "white",
    }
    parts = []
    last_end = 0
    current_style = {}
    for match in ANSI_ESCAPE.finditer(text):
        content = text[last_end : match.start()]
        if content:
            parts.append(html.Span(content, style=current_style.copy()))
        seq = match.group()
        if seq.startswith("\x1b["):
            codes = seq[2:-1].split(";")
            for code in codes:
                if code == "0" or code == "":
                    current_style = {}
                elif code == "1":
                    current_style["fontWeight"] = "bold"
                elif code in color_map:
                    current_style["color"] = color_map[code]
        last_end = match.end()
    content = text[last_end:]
    if content:
        parts.append(html.Span(content, style=current_style))
    return parts


# Background thread for data collection
def collect_stats():
    global data_store
    last_log_ts = int(time.time())
    while True:
        if data_store["container_name"]:
            try:
                container = client.containers.get(data_store["container_name"])
                stats = container.stats(stream=False)
                assert isinstance(stats, dict)
                cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
                system_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
                num_cpus = stats["cpu_stats"].get("online_cpus", len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])))
                cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta > 0 else 0.0
                mem_mib = get_mem_mib(stats)
                try:
                    log_bytes = container.logs(since=last_log_ts, stdout=True, stderr=True)
                    last_log_ts = int(time.time())
                    log_text = log_bytes.decode("utf-8", errors="replace").strip()
                    if log_text:
                        lines = log_text.split("\n")
                        cleaned = [re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d+Z\s*", "", l).strip() for l in lines if l.strip()]
                        log_display = "\n".join(cleaned)
                    else:
                        log_display = ""
                except:
                    log_display = ""
                now = datetime.now()
                data_store["timestamps"].append(now)
                data_store["cpu"].append(cpu_percent)
                data_store["mem"].append(mem_mib)
                data_store["logs"].append(log_display)
            except Exception as e:
                print(f"Error: {e}")
        else:
            last_log_ts = int(time.time())
        time.sleep(1)


thread = threading.Thread(target=collect_stats, daemon=True)
thread.start()

# Initialize Dash app
app = dash.Dash(__name__, external_stylesheets=[themes.BOOTSTRAP])

app.layout = Container(
    [
        Row([Col(html.H2("Docker Real-Time Monitor", className="text-center my-3"), width=12)]),
        Row(
            [
                # Left Side: Controls and Chart
                Col(
                    [
                        Row(
                            [
                                Col(
                                    [
                                        Label("Container:"),
                                        Dropdown(id="container-selector", placeholder="Select..."),
                                        Button("Refresh", id="refresh-btn", color="secondary", size="sm", className="mt-1"),
                                    ],
                                    width=12,
                                    className="mb-2",
                                ),
                                Col(
                                    [
                                        Label("Control:"),
                                        html.Br(),
                                        Button("Pause", id="pause-btn", color="warning", n_clicks=0, className="w-100"),
                                        Store(id="pause-state", data=False),
                                        html.Div(id="dummy-output", style={"display": "none"}),
                                    ],
                                    width=12,
                                ),
                            ],
                            className="mb-3",
                        ),
                        Graph(id="live-chart", config={"displaylogo": False}, style={"height": "500px"}),
                    ],
                    width=6,
                ),
                # Right Side: Logs
                Col(
                    [
                        html.H5("Recent Activity (Last 5 Minutes)"),
                        html.Div(id="table-container", style={"maxHeight": "85vh", "overflowY": "auto", "border": "1px solid #444"}),
                        Interval(id="interval-component", interval=1 * 1000, n_intervals=0),
                    ],
                    width=6,
                ),
            ]
        ),
    ],
    fluid=True,
)


@app.callback(
    [Output("pause-state", "data"), Output("pause-btn", "children"), Output("pause-btn", "color")],
    Input("pause-btn", "n_clicks"),
    State("pause-state", "data"),
    prevent_initial_call=True,
)
def toggle_pause(n_clicks, is_paused):
    return (False, "Pause", "warning") if is_paused else (True, "Resume", "success")


@app.callback(Output("container-selector", "options"), Input("refresh-btn", "n_clicks"))
def update_container_list(n):
    return [{"label": c.name, "value": c.name} for c in client.containers.list()]


@app.callback(Output("dummy-output", "children"), Input("container-selector", "value"))
def select_container(value):
    if value and data_store["container_name"] != value:
        data_store["timestamps"].clear()
        data_store["cpu"].clear()
        data_store["mem"].clear()
        data_store["logs"].clear()
        data_store["container_name"] = value
    elif not value:
        data_store["container_name"] = None
    return ""


@app.callback(
    [Output("live-chart", "figure"), Output("table-container", "children")],
    [Input("interval-component", "n_intervals")],
    [State("pause-state", "data")],
)
def update_outputs(n, is_paused):
    if is_paused:
        return dash.no_update, dash.no_update

    fig = go.Figure()
    table_rows = []
    if data_store["container_name"] and data_store["timestamps"]:
        all_ts = list(data_store["timestamps"])
        all_cpu = list(data_store["cpu"])
        all_mem = list(data_store["mem"])
        all_logs = list(data_store["logs"])

        last_entry = None
        is_last_placeholder = False
        for i in range(len(all_ts)):
            # Detect changes (CPU rounded to 0 decimal, Mem to integer, and exact logs)
            current_entry = (round(all_cpu[i], 0), int(all_mem[i]), all_logs[i])

            if last_entry and current_entry == last_entry:
                if is_last_placeholder:
                    continue
                placeholder_row = html.Tr([html.Td("...", style={"color": "#666", "fontSize": "0.7rem"}), html.Td(""), html.Td(""), html.Td("")])
                table_rows.append(placeholder_row)
                is_last_placeholder = True
            else:
                log_text = all_logs[i]
                lines = log_text.split("\n")

                if len(lines) > 1:
                    last_line = strip_ansi(lines[-1])
                    summary_title = "... " + last_line[:120] + ("…" if len(last_line) > 120 else "")
                    # Collapsible view for multi-line logs
                    log_display = html.Details(
                        [
                            html.Summary(
                                summary_title,
                                style={"cursor": "pointer", "outline": "none"},
                            ),
                            html.Div(
                                ansi_to_dash(log_text),
                                style={
                                    "marginTop": "8px",
                                    "paddingTop": "8px",
                                    "borderTop": "1px solid #444",
                                    "color": "#ccc",
                                },
                            ),
                        ]
                    )
                elif len(strip_ansi(log_text)) > 120:
                    log_display = html.Details(
                        [
                            html.Summary(
                                strip_ansi(log_text)[:120] + "…",
                                style={"cursor": "pointer", "outline": "none"},
                            ),
                            html.Div(
                                ansi_to_dash(log_text),
                                style={
                                    "marginTop": "8px",
                                    "paddingTop": "8px",
                                    "borderTop": "1px solid #444",
                                    "color": "#ccc",
                                },
                            ),
                        ]
                    )
                else:
                    log_display = html.Div(ansi_to_dash(log_text))

                table_rows.append(
                    html.Tr(
                        [
                            html.Td(all_ts[i].strftime("%H:%M:%S"), style={"fontSize": "0.8rem"}),
                            html.Td(f"{all_cpu[i]:.0f}%", style={"fontSize": "0.8rem"}),
                            html.Td(f"{int(all_mem[i])}M", style={"fontSize": "0.8rem"}),
                            html.Td(
                                log_display,
                                style={"whiteSpace": "pre-line", "fontFamily": "monospace", "fontSize": "0.75rem"},
                            ),
                        ]
                    )
                )
                last_entry = current_entry
                is_last_placeholder = False

        fig.add_trace(
            go.Scatter(x=all_ts, y=all_cpu, name="CPU (%)", line=dict(color="#FFD700"), yaxis="y2", hovertemplate="CPU: %{y:.1f}%<extra></extra>")
        )
        fig.add_trace(
            go.Scatter(
                x=all_ts, y=all_mem, name="Memory (MiB)", line=dict(color="#00FFFF"), yaxis="y1", hovertemplate="RAM: %{y:.0f}MiB<extra></extra>"
            )
        )

    sticky_header_style = {"position": "sticky", "top": 0, "backgroundColor": "#333", "zIndex": 10}
    table = Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Time", style=sticky_header_style),
                        html.Th("C%", style=sticky_header_style),
                        html.Th("M", style=sticky_header_style),
                        html.Th("Logs", style=sticky_header_style),
                    ]
                )
            ),
            html.Tbody(table_rows),
        ],
        bordered=True,
        color="dark",
        hover=True,
        size="sm",
        responsive=True,
        striped=True,
    )

    title = f"Live Stats: {data_store['container_name']}" if data_store["container_name"] else "Select a container"
    fig.update_layout(
        title=title,
        template="plotly_dark",
        margin=dict(l=50, r=50, t=50, b=50),
        xaxis=dict(title="Time"),
        yaxis=dict(title="Memory (MiB)", side="left", rangemode="tozero"),
        yaxis2=dict(title="CPU (%)", side="right", overlaying="y", rangemode="tozero"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig, table


if __name__ == "__main__":
    app.run(debug=True, port=8050)
