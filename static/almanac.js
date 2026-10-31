// The console's Almanac panel: what the recent drives found, drawn with d3. It
// shares /api/atlas with the drive replay, fetched the first time it's opened.
(() => {
  "use strict";

  const KINDS = [["wifi", "Wi-Fi", "#05d9e8"], ["ble", "BLE", "#9d4edd"], ["bt", "Bluetooth", "#f9c80e"]];
  const CYAN = "#05d9e8", SEEN = "#3a2a6e";
  const num = d3.format(","), si = d3.format("~s"), pct = d3.format(".1%");
  const when = d3.timeFormat("%b %-d %H:%M"), day = d3.timeFormat("%b %-d");
  const $ = id => document.getElementById(id);
  const panel = $("alm-panel"), tip = $("alm-tip");
  let data = null, focus = null, asked = false;

  function showTip(html, e) {
    tip.innerHTML = html; tip.hidden = false;
    tip.style.left = `${Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${Math.max(8, Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8))}px`;
  }
  const hideTip = () => { tip.hidden = true; };
  const driveName = d => d.start ? when(new Date(d.start * 1000)) : d.date;
  const chosen = () => focus === null ? data.points.map((c, i) => [c, data.drives[i]]) : [[data.points[focus], data.drives[focus]]];
  const scope = () => focus === null ? "all drives" : driveName(data.drives[focus]);
  const widthOf = id => Math.max(280, $(id).clientWidth);

  function svgIn(id, w, h, label) {
    const box = $(id); box.innerHTML = "";
    return d3.select(box).append("svg").attr("viewBox", `0 0 ${w} ${h}`).attr("role", "img").attr("aria-label", label);
  }
  function axes(svg, x, y, w, h, m) {
    svg.append("g").attr("class", "ax").attr("transform", `translate(0,${h - m.b})`)
      .call(d3.axisBottom(x).ticks(Math.floor(w / 90)).tickSize(3)).select(".domain").remove();
    svg.append("g").attr("class", "ax").attr("transform", `translate(${m.l},0)`)
      .call(d3.axisLeft(y).ticks(4).tickFormat(si).tickSize(-(w - m.l - m.r))).select(".domain").remove();
  }
  const chart = (id, title) => `<div class="aa-chart"><h3>${title} · <b id="${id}-note"></b></h3><div id="${id}"></div></div>`;

  function stats() {
    const seen = d3.sum(data.drives, d => d.contacts), fresh = d3.sum(data.drives, d => d.new);
    const hours = d3.sum(data.drives, d => Math.max(0, d.end - d.start)) / 3600;
    const best = d3.greatest(data.drives, d => d.new);
    return `<div class="stat-row">${[
      ["mag", num(fresh), "New to you", `in ${data.drives.length} drives`],
      ["", num(seen), "Sightings", "repeats included"],
      ["", d3.format(".0f")(seen / Math.max(hours, .01) / 60), "Per minute", "sightings, while driving"],
      ["cy", num(best.new), "Best drive", `new on ${day(new Date(best.start * 1000))}`],
    ].map(([c, n, l, sub]) => `<div class="stat ${c}"><div class="n">${n}</div><div class="l">${l}</div><div class="sub">${sub}</div></div>`).join("")}</div>`;
  }

  function hauls() {
    const list = data.drives.map((d, i) => ({ ...d, i })).sort((a, b) => b.start - a.start);
    const w = widthOf("alm-haul"), row = 22, h = list.length * row + 24, left = 92;
    const svg = svgIn("alm-haul", w, h, "New and repeat networks per drive");
    const x = d3.scaleLinear([0, d3.max(list, d => d.contacts)], [left, w - 8]).nice();
    const y = d3.scaleBand(list.map(d => d.i), [0, h - 24]).padding(.22);
    svg.append("g").attr("class", "ax").attr("transform", `translate(0,${h - 22})`)
      .call(d3.axisBottom(x).ticks(4).tickFormat(si).tickSize(3)).select(".domain").remove();
    const g = svg.selectAll(".pick").data(list).join("g").attr("class", "pick")
      .attr("opacity", d => focus === null || focus === d.i ? 1 : .35)
      .on("click", (_, d) => { focus = focus === d.i ? null : d.i; focused(); })
      .on("mousemove", (e, d) => showTip(`<b>${driveName(d)}</b><br>${num(d.new)} new, ${num(d.contacts - d.new)} repeat`, e))
      .on("mouseleave", hideTip);
    g.append("rect").attr("width", w).attr("y", d => y(d.i) - 2).attr("height", y.bandwidth() + 4).attr("fill", "transparent");
    g.append("text").attr("x", left - 8).attr("y", d => y(d.i) + y.bandwidth() / 2).attr("dy", ".32em").attr("text-anchor", "end").text(driveName);
    g.append("rect").attr("x", left).attr("y", d => y(d.i)).attr("height", y.bandwidth()).attr("rx", 2)
      .attr("width", d => Math.max(0, x(d.new) - left - 1)).attr("fill", CYAN);
    g.append("rect").attr("x", d => x(d.new) + 1).attr("y", d => y(d.i)).attr("height", y.bandwidth()).attr("rx", 2)
      .attr("width", d => Math.max(0, x(d.contacts) - x(d.new) - 1)).attr("fill", SEEN);
    $("alm-haul").insertAdjacentHTML("beforeend", `<div class="legend"><span><i style="background:${CYAN}"></i>new to you</span><span><i style="background:${SEEN}"></i>seen before</span></div>`);
    $("alm-haul-note").textContent = "click a drive to focus";
  }

  function curve() {
    let sum = 0;
    const pts = [...data.drives].sort((a, b) => a.start - b.start).map(d => ({ d, at: new Date(d.start * 1000), total: sum += d.new }));
    const w = widthOf("alm-curve"), h = 220, m = { l: 40, r: 12, t: 10, b: 22 };
    const svg = svgIn("alm-curve", w, h, "Cumulative new networks by drive");
    const x = d3.scaleTime(d3.extent(pts, p => p.at), [m.l, w - m.r]), y = d3.scaleLinear([0, sum || 1], [h - m.b, m.t]).nice();
    axes(svg, x, y, w, h, m);
    svg.append("path").datum(pts).attr("fill", "none").attr("stroke", CYAN).attr("stroke-width", 2)
      .attr("d", d3.line(p => x(p.at), p => y(p.total)).curve(d3.curveMonotoneX));
    const dot = svg.append("circle").attr("r", 5).attr("fill", CYAN).attr("stroke", "#0e0a1e").attr("stroke-width", 2).attr("visibility", "hidden");
    svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", w - m.l - m.r).attr("height", h - m.t - m.b).attr("fill", "transparent")
      .on("mousemove", e => {
        const p = pts[d3.bisectCenter(pts.map(q => +q.at), +x.invert(d3.pointer(e)[0]))];
        dot.attr("cx", x(p.at)).attr("cy", y(p.total)).attr("visibility", "visible");
        showTip(`<b>${driveName(p.d)}</b><br>${num(p.total)} new so far`, e);
      }).on("mouseleave", () => { dot.attr("visibility", "hidden"); hideTip(); });
    $("alm-curve-note").textContent = "new to you, drive by drive";
  }

  function heat() {
    const grid = d3.range(7).map(() => new Array(24).fill(0));
    for (const [c, d] of chosen()) for (const t of c.t) {
      const at = new Date((d.start + t) * 1000); grid[at.getDay()][at.getHours()]++;
    }
    const names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const w = widthOf("alm-heat"), cell = (w - 36) / 24, h = cell * 7 + 20;
    const svg = svgIn("alm-heat", w, h, "Sightings by weekday and hour");
    const peak = d3.max(grid.flat()) || 1, colour = d3.scaleSequential(d3.interpolateRgb("#123a4a", CYAN)).domain([0, Math.sqrt(peak)]);
    grid.forEach((row, d) => {
      svg.append("text").attr("x", 30).attr("y", d * cell + cell / 2).attr("dy", ".32em").attr("text-anchor", "end").text(names[d]);
      row.forEach((v, hr) => {
        svg.append("rect").attr("x", 36 + hr * cell + 1).attr("y", d * cell + 1).attr("width", cell - 2).attr("height", cell - 2).attr("rx", 2)
          .attr("fill", v ? colour(Math.sqrt(v)) : "rgba(255,255,255,0.04)")
          .on("mousemove", e => showTip(`<b>${names[d]} ${d3.format("02")(hr)}:00</b><br>${num(v)} sightings`, e)).on("mouseleave", hideTip);
      });
    });
    for (const hr of [0, 6, 12, 18]) svg.append("text").attr("x", 36 + hr * cell + 1).attr("y", h - 5).text(`${d3.format("02")(hr)}:00`);
    $("alm-heat-note").textContent = `${scope()}, your local time`;
  }

  function far() {
    const all = [];
    for (const [c] of chosen()) c.x.forEach((x, i) => all.push(Math.hypot(x, c.y[i]) / 1000));
    all.sort(d3.ascending);
    const top = d3.quantile(all, .99) || 1, bins = d3.bin().domain([0, top]).thresholds(28)(all.filter(v => v <= top));
    const w = widthOf("alm-far"), h = 200, m = { l: 40, r: 12, t: 8, b: 22 };
    const svg = svgIn("alm-far", w, h, "Sightings by distance from the centre");
    const x = d3.scaleLinear([0, top], [m.l, w - m.r]), y = d3.scaleLinear([0, d3.max(bins, b => b.length) || 1], [h - m.b, m.t]).nice();
    axes(svg, x, y, w, h, m);
    svg.selectAll(".b").data(bins).join("rect").attr("class", "b").attr("x", b => x(b.x0) + 1).attr("width", b => Math.max(1, x(b.x1) - x(b.x0) - 2))
      .attr("y", b => y(b.length)).attr("height", b => y(0) - y(b.length)).attr("rx", 2).attr("fill", CYAN)
      .on("mousemove", (e, b) => showTip(`<b>${b.x0}-${b.x1} km</b><br>${num(b.length)} sightings`, e)).on("mouseleave", hideTip);
    $("alm-far-note").textContent = `${scope()}, km, farthest 1% left off`;
  }

  function mix() {
    const totals = [0, 0, 0];
    for (const [, d] of chosen()) KINDS.forEach(([k], i) => { totals[i] += d.kinds[k]; });
    const all = d3.sum(totals) || 1, w = widthOf("alm-mix"), x = d3.scaleLinear([0, all], [0, w]);
    const svg = svgIn("alm-mix", w, 24, "Share of each network type");
    let at = 0;
    totals.forEach((v, i) => {
      if (!v) return;
      svg.append("rect").attr("x", x(at)).attr("width", Math.max(2, x(v) - 2)).attr("height", 24).attr("rx", 3).attr("fill", KINDS[i][2])
        .on("mousemove", e => showTip(`<b>${KINDS[i][1]}</b><br>${num(v)} (${pct(v / all)})`, e)).on("mouseleave", hideTip);
      at += v;
    });
    $("alm-mix").insertAdjacentHTML("beforeend", `<div class="legend">${totals.map((v, i) =>
      `<span><i style="background:${KINDS[i][2]}"></i>${KINDS[i][1]} ${pct(v / all)}</span>`).join("")}</div>`);
    $("alm-mix-note").textContent = scope();
  }

  function table() {
    $("alm-table").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Drive</th><th class="num">Sightings</th><th class="num">New</th>${
      KINDS.map(k => `<th class="num">${k[1]}</th>`).join("")}</tr></thead><tbody>${
      data.drives.map(d => `<tr><td>${driveName(d)}</td><td class="num">${num(d.contacts)}</td><td class="num">${num(d.new)}</td>${
        KINDS.map(k => `<td class="num">${num(d.kinds[k[0]])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    $("alm-table-note").textContent = "the same numbers";
  }

  function focused() { hauls(); heat(); far(); mix(); }

  function draw() {
    $("alm-body").innerHTML = `${stats()}<div class="aa-grid">${
      chart("alm-haul", "Each drive's haul") + chart("alm-curve", "Discovery so far") + chart("alm-heat", "When you find networks") +
      chart("alm-far", "How far from the centre") + chart("alm-mix", "What's out there") + chart("alm-table", "Drives")}</div>`;
    $("alm-meta").textContent = `${data.drives.length} drives · streets from ${data.ghost_drives} older`;
    curve(); table(); focused();
  }

  async function load() {
    asked = true;
    $("alm-body").innerHTML = '<div class="empty">Loading drives…</div>';
    try {
      const res = await fetch("/api/atlas");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (e) {
      asked = false;
      $("alm-body").innerHTML = '<div class="src-err">Couldn\'t load the drives. Close and reopen to try again.</div>';
      return;
    }
    if (!data.drives.length) {
      $("alm-body").innerHTML = '<div class="empty">No drives yet: they appear once WiGLE has processed an upload.</div>';
      return;
    }
    draw();
  }

  // Open or shut, as the viewer left it last time, like After Action.
  panel.addEventListener("toggle", () => {
    try { localStorage.setItem("alm-open", panel.open ? "1" : "0"); } catch (e) {}
    if (panel.open && !asked) load();
    if (!panel.open) hideTip();
  });
  try { panel.open = localStorage.getItem("alm-open") === "1"; } catch (e) {}
})();
