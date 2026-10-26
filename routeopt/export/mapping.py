"""Folium map of a plan (brief 8.4): hub, colored trucks, numbered stops, polylines."""

from __future__ import annotations

from collections.abc import Callable

import folium
from folium.plugins import Fullscreen

from routeopt.config import RunSettings
from routeopt.domain import (
    DEFAULT_SITE,
    HUB_LAT,
    HUB_LON,
    SITE_BY_CODE,
    SITES,
    Plan,
    Stop,
    clock,
)

Coord = tuple[float, float]
GeometryFn = Callable[[list[Coord]], list[Coord] | None]

PALETTE = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
    "#e377c2",
    "#8c564b",
    "#bcbd22",
    "#7f7f7f",
    "#003f5c",
    "#bc5090",
    "#ffa600",
    "#58508d",
]


def truck_colors(plan: Plan) -> dict[str, str]:
    """Stable color per truck number (palette order = truck order)."""
    return {t.number: PALETTE[i % len(PALETTE)] for i, t in enumerate(plan.trucks)}


def _badge(label: str, color: str) -> folium.DivIcon:
    html = (
        f'<div style="background:{color};color:white;border:2px solid white;border-radius:12px;'
        f"padding:1px 6px;font:bold 11px Arial;white-space:nowrap;box-shadow:0 0 3px #333;"
        f'display:inline-block">{label}</div>'
    )
    return folium.DivIcon(html=html, icon_size=(1, 1), icon_anchor=(0, 0))


def _popup(stop: Stop | None, stop_id: str, extra: str) -> str:
    if stop is None:
        return f"<b>{stop_id}</b><br/>{extra}"
    shipments = "<br/>".join(
        f"&nbsp;&nbsp;{sh.shipment_id}: {sh.model_code or ''} x {sh.pieces} ({sh.shipment_type.value})"
        for sh in stop.shipments
    )
    return (
        f"<b>{stop.customer_label}</b><br/>{stop.full_address}<br/>"
        f"tier {stop.tier} · {stop.day_tag.value} · {stop.delivery_volume:.0f} cu ft out"
        + (f" / {stop.pickup_volume:.0f} cu ft in" if stop.pickup_volume else "")
        + f" · {stop.service_total_minutes} min<br/>{extra}<br/>{shipments}"
    )


def build_map(
    plan: Plan,
    stops: list[Stop],
    settings: RunSettings,
    geometry_fn: GeometryFn | None = None,
) -> folium.Map:
    """Return a folium map of the plan; ``geometry_fn`` supplies road polylines (OSRM)."""
    by_id = {s.stop_id: s for s in stops}
    colors = truck_colors(plan)
    m = folium.Map(
        location=[HUB_LAT, HUB_LON], zoom_start=10, tiles="OpenStreetMap", control_scale=True
    )
    Fullscreen().add_to(m)
    points: list[Coord] = [(HUB_LAT, HUB_LON)]
    used_sites = {load.site for t in plan.trucks for load in t.loads if load.stops} or {
        DEFAULT_SITE
    }
    for site in SITES:
        if site.code not in used_sites:
            continue
        folium.Marker(
            [site.lat, site.lon],
            popup=f"<b>{site.name}</b><br/>{site.address}",
            tooltip=f"Hub · {site.name}",
            icon=folium.Icon(color="black", icon="home", prefix="fa"),
        ).add_to(m)
        points.append((site.lat, site.lon))

    for truck in plan.trucks:
        loads = [load for load in truck.loads if load.stops]
        if not loads:
            continue
        color = colors[truck.number]
        group = folium.FeatureGroup(
            name=f"{truck.number} – {truck.driver_name or 'no driver'}", show=True
        )
        for load in loads:
            depot = SITE_BY_CODE[load.site]
            coords: list[Coord] = [(depot.lat, depot.lon)]
            for visit in load.stops:
                stop = by_id.get(visit.stop_id)
                if stop is None:
                    continue
                coords.append((stop.latitude, stop.longitude))
                label = f"{truck.number}·L{load.seq}·#{visit.seq}"
                extra = (
                    f"ETA {clock(visit.arrive_s)} (window {clock(visit.eta_window_start_s)}-"
                    f"{clock(visit.eta_window_end_s)}) · volume after {visit.volume_after:.0f} cu ft"
                    + (" · lunch before this stop" if visit.lunch_before else "")
                )
                folium.Marker(
                    [stop.latitude, stop.longitude],
                    icon=_badge(label, color),
                    tooltip=f"{label} {stop.customer_label}",
                    popup=folium.Popup(_popup(stop, visit.stop_id, extra), max_width=360),
                ).add_to(group)
                points.append((stop.latitude, stop.longitude))
            coords.append((depot.lat, depot.lon))
            line = geometry_fn(coords) if geometry_fn is not None else None
            folium.PolyLine(
                line or coords,
                color=color,
                weight=4 if load.seq == 1 else 3,
                opacity=0.85,
                dash_array=None if load.seq == 1 else "8,6",
                tooltip=f"{truck.number} load {load.seq} ({depot.name}): "
                f"{clock(load.depart_s)}-{clock(load.return_s)}",
            ).add_to(group)
        group.add_to(m)

    if plan.unassigned:
        group = folium.FeatureGroup(name="Unassigned", show=True)
        for u in plan.unassigned:
            stop = by_id.get(u.stop_id)
            if stop is None:
                continue
            folium.CircleMarker(
                [stop.latitude, stop.longitude],
                radius=7,
                color="#555555",
                fill=True,
                fill_color="#9e9e9e",
                fill_opacity=0.9,
                tooltip=f"Unassigned: {stop.customer_label}",
                popup=folium.Popup(
                    _popup(stop, u.stop_id, f"<b>{u.reason}</b> {u.detail}"), max_width=360
                ),
            ).add_to(group)
            points.append((stop.latitude, stop.longitude))
        group.add_to(m)
    if plan.out_of_scope:
        group = folium.FeatureGroup(name="Not in scope", show=False)
        for u in plan.out_of_scope:
            stop = by_id.get(u.stop_id)
            if stop is None:
                continue
            folium.CircleMarker(
                [stop.latitude, stop.longitude],
                radius=5,
                color="#bbbbbb",
                fill=True,
                fill_color="#dddddd",
                fill_opacity=0.8,
                tooltip=f"Not in scope: {stop.customer_label} ({u.reason})",
            ).add_to(group)
        group.add_to(m)

    legend_rows = "".join(
        f'<div><span style="display:inline-block;width:12px;height:12px;background:{colors[t.number]};'
        f'margin-right:6px;border-radius:2px"></span>{t.number} {t.driver_name}</div>'
        for t in plan.trucks
        if t.used
    )
    legend_rows += (
        '<div><span style="display:inline-block;width:12px;height:12px;background:#9e9e9e;'
        'margin-right:6px;border-radius:6px"></span>Unassigned</div>'
    )
    legend = (
        '<div style="position:fixed;bottom:24px;left:12px;z-index:9999;background:white;'
        'padding:8px 10px;border:1px solid #888;border-radius:6px;font:12px Arial;max-height:40%;overflow:auto">'
        f"<b>Trucks</b>{legend_rows}</div>"
    )
    m.get_root().html.add_child(folium.Element(legend))  # type: ignore[attr-defined]
    folium.LayerControl(collapsed=False).add_to(m)
    if len(points) > 1:
        m.fit_bounds(
            [
                [min(p[0] for p in points), min(p[1] for p in points)],
                [max(p[0] for p in points), max(p[1] for p in points)],
            ]
        )
    return m


def map_html(
    plan: Plan, stops: list[Stop], settings: RunSettings, geometry_fn: GeometryFn | None = None
) -> str:
    """The map as a standalone HTML document."""
    return str(build_map(plan, stops, settings, geometry_fn).get_root().render())
