"""Greater Boston heat-risk map (Shiny for Python).

Run with:  shiny run app.py --launch-browser

Shiny is reactive: data loads once at startup, and moving a control only recomputes
what depends on it. The map is created once; slider changes just recolour the tracts,
so your zoom and position are kept.
"""
import json
import sys
from pathlib import Path

import ipyleaflet as L
import requests
from ipywidgets import Layout
from shiny import App, reactive, render, req, ui
from shinywidgets import output_widget, render_widget

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "src"))
import data  # noqa: E402
import score  # noqa: E402

# ---------- loaded once at startup ----------
BASE = score.load_base()
BASE["geometry"] = BASE.geometry.simplify(0.0001)  # lighter polygons, no visible change
GEOJSON = json.loads(BASE[["GEOID", "geometry"]].to_json())
SITES = data.load_sites()
SITES_GEOJSON = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {"name": s.name, "kind": s.kind},
                  "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]}}
                 for s in SITES.itertuples()],
}

# Dark-map palette: dim indigo (low) -> glowing amber (high). Brighter = more urgent.
BINS = [(80, "#fcd14d"), (60, "#f0703f"), (40, "#c2427f"), (20, "#7a3ea8"), (0, "#3b3f8f")]
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"


def risk_color(risk: float) -> str:
    return next(color for lo, color in BINS if risk >= lo)


def tract_label(geoid: str) -> str:
    return f"Tract {geoid[5:9].lstrip('0')}.{geoid[9:]}"


LOGO = ui.HTML(
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" '
    'stroke-linecap="round"><path d="M12 3v11"/><circle cx="12" cy="17.5" r="3.5" fill="#fff"/>'
    '<path d="M16 5h3M16 9h3"/></svg>'
)


def geocode(q: str):
    """Look up a place with OpenStreetMap's Nominatim, biased to Greater Boston."""
    s, w, n, e = data.BBOX
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": q, "format": "json", "limit": 1,
                             "viewbox": f"{w},{n},{e},{s}", "bounded": 1},
                     headers={"User-Agent": "heat-risk-map-practice/0.1"}, timeout=15)
    r.raise_for_status()
    hits = r.json()
    return (float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"]) if hits else None


def stat(label, output_id):
    return ui.div(ui.div(label, class_="stat-label"),
                  ui.div(ui.output_text(output_id, inline=True), class_="stat-value"), class_="stat")


# ---------- layout: full-screen map with glass overlays ----------
weight_sliders = [ui.input_slider(f"w_{k}", label, 0, 2, 1, step=0.25)
                  for k, label in score.FACTORS.items()]

app_ui = ui.page_fillable(
    ui.head_content(
        ui.tags.link(rel="preconnect", href="https://fonts.googleapis.com"),
        ui.tags.link(rel="stylesheet",
                     href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"),
        ui.include_css(HERE / "www" / "styles.css"),
        ui.tags.script("document.documentElement.setAttribute('data-bs-theme','dark');"
                       # press Enter in the search box to search
                       "document.addEventListener('keydown', e => {"
                       "  if (e.key !== 'Enter' || e.target.id !== 'query') return;"
                       # send the typed text right away (Shiny debounces text inputs), then search
                       "  Shiny.setInputValue('query', e.target.value, {priority: 'event'});"
                       "  document.getElementById('go').click();"
                       "});"),
    ),
    ui.div(output_widget("map", height="100vh"), id="map-wrap"),

    # top bar
    ui.div(
        ui.div(ui.div(LOGO, class_="brand-mark"), "HeatRisk",
               ui.span("Greater Boston", class_="brand-sub"), class_="brand"),
        ui.div(ui.input_text("query", None, placeholder="Search an address or place…"),
               ui.input_action_button("go", "Find", class_="btn-accent"),
               ui.output_ui("search_msg"), class_="search"),
        ui.div(ui.output_ui("mode_pill"),
               ui.download_button("download", "Download CSV", class_="btn-accent"),
               class_="topbar-right"),
        id="topbar", class_="glass",
    ),

    # left: story + controls
    ui.div(
        ui.h1("Who needs help first when the heat hits?", class_="headline"),
        ui.p("Census tracts ranked by heat hazard, social vulnerability and density. "
             "Click any tract to see why it scores high.", class_="lede"),
        ui.div("Risk score", class_="section-label"),
        ui.div(class_="legend-bar"),
        ui.div(*[ui.span(str(v)) for v in (0, 20, 40, 60, 80, 100)], class_="legend-ticks"),
        ui.div("Scenario", class_="section-label"),
        ui.input_radio_buttons("mode", None,
                               {"scenario": "Heat-wave scenario", "forecast": "Live 7-day forecast"}),
        ui.panel_conditional("input.mode === 'scenario'",
                             ui.input_slider("temp", "Feels-like temperature (°C)", 26, 45, 37),
                             ui.p("37 °C ≈ Boston's July 2022 heat wave peak.", class_="hint")),
        ui.panel_conditional("input.mode === 'forecast'",
                             ui.input_slider("delta", "Add degrees to forecast (°C)", 0, 15, 0)),
        ui.input_checkbox("outage", "Assume a power outage"),
        ui.p("Assumption: AC fails, so heat exposure rises 30%.", class_="hint"),
        ui.output_ui("mild_notice"),
        ui.accordion(ui.accordion_panel("Factor weights", *weight_sliders), open=False),
        id="controls", class_="glass",
    ),

    # top-centre stats
    ui.div(
        stat("Feels-like peak", "vb_temp"),
        stat("High-risk tracts", "vb_high"),
        stat("At-risk 65+", "vb_people"),
        stat("Highest score", "vb_max"),
        id="stats", class_="glass",
    ),

    # right: selection details
    ui.div(ui.output_ui("details"), id="details-panel", class_="glass"),

    # bottom drawer: ranked list
    ui.tags.details(
        ui.tags.summary("Top 10 tracts for welfare checks"),
        ui.div(ui.output_data_frame("top10"), class_="drawer-body"),
        id="drawer", class_="glass",
    ),
    padding=0,
    gap=0,
    title="HeatRisk · Greater Boston",
)


# ---------- server ----------
def server(input, output, session):
    selected = reactive.value(None)  # GEOID of the clicked tract
    selected_site = reactive.value(None)  # properties of the clicked cooling site

    @reactive.calc
    def weights():
        w = {k: input[f"w_{k}"]() for k in score.FACTORS}
        req(sum(w.values()) > 0)  # pause outputs if every weight is 0
        return w

    @reactive.calc
    def scored():
        if input.mode() == "scenario":
            kw = {"scenario_temp_c": input.temp()}
        else:
            kw = {"delta_c": input.delta()}
        return score.compute(BASE, weights=weights(), outage=input.outage(), **kw)

    # --- headline numbers
    @render.text
    def vb_temp():
        return f"{scored()['feels_like_c'].max():.1f} °C"

    @render.text
    def vb_high():
        return str(int((scored()["risk"] >= 60).sum()))

    @render.text
    def vb_people():
        return f"{scored()['at_risk_pop'].sum():,}"

    @render.text
    def vb_max():
        return f"{scored()['risk'].max():.0f}"

    @render.ui
    def mode_pill():
        src = f"{input.temp()} °C scenario" if input.mode() == "scenario" else "live forecast"
        return ui.span("Showing ", ui.tags.b(src), " · outage on" if input.outage() else "", class_="pill")

    @render.ui
    def mild_notice():
        if scored()["risk"].max() == 0:
            return ui.div("The forecast isn't hot enough to create risk. Switch to the heat-wave "
                          "scenario or add degrees.", class_="notice")

    # --- map: built once, never re-rendered
    @render_widget
    def map():
        m = L.Map(center=(42.335, -71.08), zoom=12, scroll_wheel_zoom=True, zoom_control=False,
                  basemap=L.TileLayer(url=ESRI.format("Canvas/World_Dark_Gray_Base"),
                                      attribution="Esri, HERE, Garmin, © OpenStreetMap contributors",
                                      name="Dark", base=True),
                  layout=Layout(height="100vh", width="100%"))
        for name, bm in [("Streets", L.basemaps.OpenStreetMap.Mapnik),
                         ("Satellite", L.basemaps.Esri.WorldImagery)]:
            tiles = L.basemap_to_tiles(bm)
            tiles.name, tiles.base, tiles.visible = name, True, False  # switch via layer control
            m.add(tiles)

        tracts = L.GeoJSON(data=GEOJSON, name="Heat risk",
                           style={"color": "#ffffff", "opacity": 0.12, "weight": 0.6, "fillOpacity": 0.5},
                           hover_style={"weight": 2, "color": "#ffffff", "opacity": 0.9, "fillOpacity": 0.85})
        tracts.on_click(lambda feature=None, **_: selected.set(feature["properties"]["GEOID"]))
        m.add(tracts)

        # street/place labels drawn above the colours so you can still read the map
        m.add(L.TileLayer(url=ESRI.format("Canvas/World_Dark_Gray_Reference"), name="Labels",
                          attribution=""))

        sites = L.GeoJSON(data=SITES_GEOJSON, name="Candidate cooling sites",
                          point_style={"radius": 4, "color": "#ffffff", "weight": 1,
                                       "fillColor": "#8083ff", "fillOpacity": 0.95},
                          hover_style={"radius": 7, "fillColor": "#ffffff"})
        sites.on_click(lambda feature=None, **_: selected_site.set(feature["properties"]))
        m.add(sites)

        m.add(L.ZoomControl(position="bottomright"))
        m.add(L.LayersControl(position="bottomright"))
        return m

    # --- address search (top bar)
    search_status = reactive.value("")

    @reactive.effect
    @reactive.event(input.go)
    def find_place():
        q = input.query().strip()
        req(q)
        try:
            hit = geocode(q)
        except requests.RequestException:
            search_status.set("Search service unavailable")
            return
        if hit is None:
            search_status.set("Not found in Greater Boston")
            return
        lat, lon, name = hit
        m = map.widget
        pin = next((layer for layer in m.layers if layer.name == "Search result"), None)
        if pin is None:
            m.add(L.Marker(location=(lat, lon), name="Search result", draggable=False, title=name))
        else:
            pin.location = (lat, lon)
        m.center, m.zoom = (lat, lon), 15
        search_status.set("")

    @render.ui
    def search_msg():
        msg = search_status()
        return ui.span(msg, class_="search-msg") if msg else None

    @reactive.effect
    def recolor():
        """Recolour tracts in place when the score changes (keeps zoom and position)."""
        m = map.widget
        req(m is not None)
        tracts = next(layer for layer in m.layers if layer.name == "Heat risk")
        risk = scored().set_index("GEOID")["risk"]

        def style(f):
            r = risk.get(f["properties"]["GEOID"], 0)
            return {"fillColor": risk_color(r), "fillOpacity": 0.25 + 0.5 * r / 100}

        tracts.style_callback = style
        tracts.data = dict(GEOJSON)  # reassigning data re-applies the style

    # --- details panel
    @render.ui
    def details():
        site = selected_site()
        site_block = []
        if site:
            kind = "Library" if site["kind"] == "library" else "Community centre"
            site_block = [ui.div("Cooling site", class_="section-label"),
                          ui.div(ui.tags.b(site["name"]), ui.br(),
                                 ui.span(kind, style="color:var(--muted);font-size:12px;"))]

        geoid = selected()
        if geoid is None:
            return ui.div(
                ui.div("Selected tract", class_="section-label", style="margin-top:0"),
                ui.p("Click a tract on the map to see its score and the reasons behind it. "
                     "Purple dots are libraries and community centres that could serve as cooling sites.",
                     class_="empty"),
                *site_block,
            )
        row = scored().set_index("GEOID").loc[geoid]
        items = [
            ui.div("Selected tract", class_="section-label", style="margin-top:0"),
            ui.div(tract_label(geoid), class_="tract-name"),
            ui.div(f"{row['risk']:.0f}", ui.tags.small(" / 100"), class_="tract-score",
                   style=f"color:{risk_color(row['risk'])};text-shadow:0 0 24px {risk_color(row['risk'])}66;"),
            ui.div(ui.span("Population"), ui.span(f"{row['pop']:,.0f}"), class_="kv"),
            ui.div(ui.span("Residents 65+"), ui.span(f"{row['pop65']:,.0f}"), class_="kv"),
            ui.div(ui.span("At-risk 65+"), ui.span(f"{row['at_risk_pop']:,}"), class_="kv"),
            ui.div(ui.span("Feels-like"), ui.span(f"{row['feels_like_c']:.1f} °C"), class_="kv"),
            ui.div("Why it scores high", class_="section-label"),
            ui.tags.ul(*[ui.tags.li(p) for p in row["drivers"].split("; ") if p], class_="why"),
        ]
        if row["low_confidence"]:
            items.append(ui.div("⚠ Small population: percentages are noisy.", class_="warn"))
        return ui.div(*items, *site_block)

    # --- table + download
    @render.data_frame
    def top10():
        t = scored().nlargest(10, "risk")[["GEOID", "risk", "pop65", "at_risk_pop", "drivers"]]
        t["GEOID"] = t["GEOID"].map(tract_label)
        t.columns = ["Tract", "Risk", "Residents 65+", "At-risk 65+", "Why"]
        return render.DataGrid(t, width="100%")

    @render.download(filename="heat_risk_tracts.csv")
    def download():
        yield scored().drop(columns="geometry").sort_values("risk", ascending=False).to_csv(index=False)


app = App(app_ui, server)
