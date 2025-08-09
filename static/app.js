const { useEffect, useRef, useState, useMemo } = React;

function msToClock(ms) {
  if (ms <= 0) return "00:00";
  const s = Math.floor(ms / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function daysToMonthsDays(days) {
  // Align ticks to average month length so marks and labels match better
  const avgMonth = 365 / 12; // ≈30.4167
  const monthsFloat = days / avgMonth;
  let months = Math.floor(monthsFloat + 1e-9);
  let remainder = days - months * avgMonth;
  let remDays = Math.round(remainder);
  const avgMonthRounded = Math.round(avgMonth);
  if (remDays >= avgMonthRounded) {
    months += 1;
    remDays = 0;
  }
  // Clamp to 0..12 months
  if (months > 12) { months = 12; remDays = 0; }
  if (months < 0) { months = 0; }
  return { months, days: remDays };
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error("request failed");
  return await res.json();
}

function useRegistration() {
  const [player, setPlayer] = useState(() => {
    const raw = localStorage.getItem("ageoguessr_player");
    return raw ? JSON.parse(raw) : null;
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!player) {
        const created = await api("/api/register", { method: "POST", body: JSON.stringify({}) });
        if (!cancelled) {
          setPlayer(created);
          localStorage.setItem("ageoguessr_player", JSON.stringify(created));
        }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const updateUsername = async (username) => {
    const pid = player?.player_id;
    if (!pid) return;
    const res = await api("/api/username", { method: "POST", body: JSON.stringify({ player_id: pid, username }) });
    const updated = { ...player, username };
    setPlayer(updated);
    localStorage.setItem("ageoguessr_player", JSON.stringify(updated));
    return res;
  };

  return { player, setPlayer, updateUsername };
}

function CircularTimer({ endsAtMs, totalDurationMs, nowMs }) {
  const deltaMs = endsAtMs ? Math.max(0, endsAtMs - nowMs) : 0;
  const progress = totalDurationMs > 0 ? Math.max(0, Math.min(100, ((totalDurationMs - deltaMs) / totalDurationMs) * 100)) : 0;
  const isUrgent = deltaMs <= 3000 && deltaMs > 0;
  
  const timeText = endsAtMs ? msToClock(deltaMs) : "--:--";
  
  return React.createElement("div", { className: "circular-timer" },
    React.createElement("div", { 
      className: `timer-circle ${isUrgent ? "urgent" : ""}`,
      style: { "--progress": `${progress}%` }
    }),
    React.createElement("div", { className: `timer-inner ${isUrgent ? "urgent" : ""}` }, timeText)
  );
}

function App() {
  const { player, updateUsername } = useRegistration();
  const [state, setState] = useState(null);
  const [nowMs, setNowMs] = useState(Date.now());
  const [tempGuessDays, setTempGuessDays] = useState(null);
  const lastSentGuessRef = useRef(null);
  const keyBufferRef = useRef("");

  // For animated personal score bump
  const [myScore, setMyScore] = useState(0);
  const [bump, setBump] = useState(null); // {points}

  // Track last seen round/phase to trigger animations
  const lastRoundRef = useRef({ round: 0, phase: "" });

  // Media cache: versioned URL -> { blobUrl, type }
  const mediaCacheRef = useRef(new Map());
  const [currentMediaSrc, setCurrentMediaSrc] = useState(null);
  const [currentMediaType, setCurrentMediaType] = useState(null);
  const currentUrlRef = useRef(null);

  async function prefetchToBlob(url, type) {
    if (!url) return null;
    const cache = mediaCacheRef.current;
    if (cache.has(url)) return cache.get(url).blobUrl;
    const res = await fetch(url, { cache: "force-cache" });
    if (!res.ok) throw new Error("media fetch failed");
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    cache.set(url, { blobUrl, type });
    // Best-effort cache trim: keep last 4
    if (cache.size > 4) {
      const firstKey = cache.keys().next().value;
      const first = cache.get(firstKey);
      try { URL.revokeObjectURL(first.blobUrl); } catch {}
      cache.delete(firstKey);
    }
    return blobUrl;
  }

  function usePoll() {
    useEffect(() => {
      let active = true;
      async function tick() {
        try {
          const data = await api("/api/state");
          if (!active) return;
          setState(data);
          setNowMs(data.server_time_ms);
        } catch (e) {
          // ignore
        }
      }
      tick();
      const h = setInterval(tick, 1000);
      return () => { active = false; clearInterval(h); };
    }, []);
  }
  usePoll();

  // Auto-join the current game when we have a player
  useEffect(() => {
    if (!player?.player_id) return;
    api("/api/join", { method: "POST", body: JSON.stringify({ player_id: player.player_id }) }).catch(() => {});
  }, [player?.player_id]);

  // When a new game starts, re-join (new game_id)
  useEffect(() => {
    if (!player?.player_id) return;
    const gid = state?.game?.game_id;
    if (!gid) return;
    api("/api/join", { method: "POST", body: JSON.stringify({ player_id: player.player_id }) }).catch(() => {});
  }, [state?.game?.game_id, player?.player_id]);

  // Local clock
  useEffect(() => {
    const t = setInterval(() => setNowMs((v) => v + 250), 250);
    return () => clearInterval(t);
  }, []);

  // Update myScore and detect reveal start to animate bump
  useEffect(() => {
    if (!state || !player) return;
    const lb = state.leaderboard || [];
    const me = lb.find((r) => r.player_id === player.player_id);
    if (me && me.score !== myScore) {
      setMyScore(me.score);
    }

    const round = state.game?.round_number || 0;
    const phase = state.game?.phase || "";
    if (phase === "reveal" && (lastRoundRef.current.round !== round || lastRoundRef.current.phase !== "reveal")) {
      lastRoundRef.current = { round, phase };
      // Compute my points from reveal block if present
      const results = state.game?.reveal?.results || {};
      const mine = results[player.player_id];
      if (mine && mine.points > 0) {
        setBump({ points: mine.points });
        setTimeout(() => setBump(null), 900);
      } else {
        setBump(null);
      }
    } else {
      lastRoundRef.current = { round, phase };
    }
  }, [state, player]);

  const endsAt = state?.game?.turn_ends_at_ms ?? null;
  const totalDurationMs = (state?.game?.turn_duration_seconds || 0) * 1000;

  const serverMediaUrl = state?.game?.media_url ?? null;
  const mediaType = state?.game?.media_type ?? null;
  const phase = state?.game?.phase ?? "";
  const babyName = state?.baby_name || "the baby";
  const mediaLabel = mediaType === "image" ? "photo" : (mediaType === "video" ? "video" : "media");
  const promptText = `How old is ${babyName} in this ${mediaLabel}?`;

  // Prefetch pending during reveal
  useEffect(() => {
    const pending = state?.game?.pending;
    if (phase === "reveal" && pending?.media_url && pending?.media_type) {
      prefetchToBlob(pending.media_url, pending.media_type).catch(() => {});
    }
  }, [phase, state?.game?.pending?.media_url]);

  // Ensure current media is sourced from cache/blob
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!serverMediaUrl || !mediaType) {
        setCurrentMediaSrc(null);
        setCurrentMediaType(null);
        currentUrlRef.current = null;
        return;
      }
      if (currentUrlRef.current === serverMediaUrl && currentMediaSrc) return;
      currentUrlRef.current = serverMediaUrl;
      // Prefer cached blob (possibly preloaded as pending)
      const cache = mediaCacheRef.current;
      if (cache.has(serverMediaUrl)) {
        const { blobUrl } = cache.get(serverMediaUrl);
        if (!cancelled) {
          setCurrentMediaSrc(blobUrl);
          setCurrentMediaType(mediaType);
        }
        return;
      }
      // Not cached yet: fetch now and then display
      try {
        const blobUrl = await prefetchToBlob(serverMediaUrl, mediaType);
        if (!cancelled) {
          setCurrentMediaSrc(blobUrl);
          setCurrentMediaType(mediaType);
        }
      } catch (e) {
        // leave as null
      }
    })();
    return () => { cancelled = true; };
  }, [serverMediaUrl, mediaType]);

  async function sendGuess(days) {
    if (!player?.player_id) return;
    if (lastSentGuessRef.current === days) return;
    lastSentGuessRef.current = days;
    try {
      await api("/api/guess", { method: "POST", body: JSON.stringify({ player_id: player.player_id, guess_days: days }) });
    } catch (e) {}
  }

  // Hidden newgame trigger by typing 'newgame'
  useEffect(() => {
    function onKey(e) {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      keyBufferRef.current = (keyBufferRef.current + (e.key || "").toLowerCase()).slice(-7);
      if (keyBufferRef.current === "newgame") {
        api("/api/newgame", { method: "POST", body: JSON.stringify({}) }).catch(() => {});
        keyBufferRef.current = "";
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Reset local guess UI on new round
  useEffect(() => {
    const round = state?.game?.round_number || 0;
    const phase = state?.game?.phase || "";
    if (phase === "guessing") {
      setTempGuessDays(null);
      lastSentGuessRef.current = null;
    }
  }, [state?.game?.round_number, state?.game?.phase]);

  const onSliderChange = (e) => {
    const val = parseInt(e.target.value, 10);
    setTempGuessDays(val);
    sendGuess(val);
  };
  const onSliderCommit = () => {
    if (tempGuessDays != null) sendGuess(tempGuessDays);
  };

  const myRowId = player?.player_id;
  const leaderboard = state?.leaderboard || [];
  const readyInfo = state?.game?.ready || null;
  const iAmReady = !!(readyInfo && myRowId && (readyInfo.ready_player_ids || []).includes(myRowId));

  async function sendReady() {
    if (!player?.player_id) return;
    try { await api("/api/ready", { method: "POST", body: JSON.stringify({ player_id: player.player_id }) }); } catch (e) {}
  }

  return (
    React.createElement("div", { className: "app-shell" },
      React.createElement("div", { className: "left-col" },
        React.createElement("div", { className: "panel header" },
          React.createElement("div", { className: "brand" }, "ageoguessr"),
          React.createElement("div", { className: "prompt" }, promptText),
          phase !== "reveal" && React.createElement(CircularTimer, { endsAtMs: endsAt, totalDurationMs, nowMs })
        ),
        React.createElement("div", { className: "panel username-box username-mobile" },
          React.createElement("span", null, "Username:"),
          React.createElement(UsernameEditor, { player, onSubmit: updateUsername })
        ),
        React.createElement("div", { className: "panel media-stage" },
          React.createElement("div", { className: "media-box" },
            state?.game?.active && currentMediaSrc ? (
              currentMediaType === "video"
                ? React.createElement("video", { src: currentMediaSrc, controls: true, autoPlay: true, loop: true })
                : React.createElement("img", { src: currentMediaSrc, alt: "current" })
            ) : (
              state?.game?.active ? React.createElement("div", null, "Loading media...") : null
            )
          ),
          phase === "reveal" && React.createElement(RevealOverlay, { state, player })
        ),
        React.createElement("div", { className: "panel controls" },
          React.createElement(GuessControl, {
            disabled: !state?.game?.active || phase !== "guessing",
            guessDays: tempGuessDays,
            onChange: onSliderChange,
            onCommit: onSliderCommit,
            reveal: phase === "reveal",
            state,
            player,
          }),
          phase === "guessing" && React.createElement("div", { className: "ready-row" },
            React.createElement("button", { className: "ready-btn", onClick: sendReady, disabled: iAmReady }, iAmReady ? "Ready ✔" : "Ready"),
            readyInfo && React.createElement("span", { className: "ready-count" }, `${readyInfo.count}/${readyInfo.total} ready`)
          )
        )
      ),
      React.createElement("div", { className: "right-col" },
        React.createElement("div", { className: "panel username-box" },
          React.createElement("span", null, "Username:"),
          React.createElement(UsernameEditor, { player, onSubmit: updateUsername })
        ),
        React.createElement("div", { className: "panel leaderboard" },
          React.createElement("h3", null, "Leaderboard"),
          leaderboard.map((row) => (
            React.createElement("div", { key: row.player_id, className: `row ${row.player_id === myRowId ? "me" : ""}` },
              React.createElement("span", null, row.username),
              React.createElement("span", null, row.score),
              bump && row.player_id === myRowId && React.createElement("span", { className: "score-bump", style: { position: 'absolute', right: 10, top: -2 } }, `+${bump.points}`)
            )
          )),
        )
      )
    )
  );
}

function RevealOverlay({ state, player }) {
  const trueDays = state?.game?.reveal?.true_age_days ?? null;
  const results = state?.game?.reveal?.results || {};
  const mine = player ? results[player.player_id] : null;
  const myGuess = mine?.guess_days;
  const myDiff = mine?.diff;
  const myPoints = mine?.points || 0;

  const idToName = Object.fromEntries((state.leaderboard || []).map((r) => [r.player_id, r.username]));
  const entries = Object.entries(results).map(([pid, r]) => ({ player_id: pid, username: idToName[pid] || pid, ...r }));
  entries.sort((a, b) => a.diff - b.diff);
  const top5 = entries.slice(0, 5);

  const { months: tm, days: td } = daysToMonthsDays(trueDays ?? 0);
  const { months: gm, days: gd } = daysToMonthsDays(myGuess ?? 0);

  return (
    React.createElement("div", { className: "reveal-overlay" },
      React.createElement("div", { className: "reveal-card" },
        React.createElement("div", { className: "reveal-title" }, "Reveal!"),
        trueDays != null && React.createElement("div", { className: "reveal-detail" }, `True age: ${tm} months, ${td} days (${trueDays} days)`),
        myGuess != null && React.createElement("div", { className: "reveal-detail" }, `Your guess: ${gm} months, ${gd} days (${myGuess} days)`),
        myDiff != null && React.createElement("div", { className: "reveal-detail" }, `Off by: ${myDiff} days`),
        React.createElement("div", { className: "reveal-points" }, `+${myPoints} points`),
        top5.length > 0 && React.createElement(React.Fragment, null,
          React.createElement("div", { className: "reveal-title", style: { marginTop: 8 } }, "Top guesses"),
          top5.map((e) => {
            const dd = daysToMonthsDays(e.guess_days || 0);
            return React.createElement("div", { key: e.player_id, className: "reveal-detail" }, `${e.username}: ${dd.months} months, ${dd.days} days (${e.guess_days} days) — off by ${e.diff}`);
          })
        )
      )
    )
  );
}

function UsernameEditor({ player, onSubmit }) {
  const [value, setValue] = useState(player?.username || "");
  useEffect(() => { setValue(player?.username || ""); }, [player?.username]);
  return (
    React.createElement(React.Fragment, null,
      React.createElement("input", {
        value,
        onChange: (e) => setValue(e.target.value),
        onBlur: () => onSubmit && onSubmit(value),
      }),
      React.createElement("button", { onClick: () => onSubmit && onSubmit(value) }, "Save")
    )
  );
}

function GuessControl({ disabled, guessDays, onChange, onCommit, reveal = false, state, player }) {
  const display = useMemo(() => daysToMonthsDays(guessDays ?? 0), [guessDays]);
  const label = `${display.months} months, ${display.days} days`;
  const marks = Array.from({ length: 13 }, (_, i) => i);
  const hasGuess = guessDays != null;

  // Build dots for reveal
  let dots = [];
  if (reveal && state?.game?.reveal) {
    const results = state.game.reveal.results || {};
    const trueDays = state.game.reveal.true_age_days;
    const myId = player?.player_id;
    // other players' guesses in blue, mine in pink
    for (const [pid, r] of Object.entries(results)) {
      const d = r.guess_days;
      if (typeof d !== "number") continue;
      dots.push({ value: d, color: "#4aa3ff", size: 10, key: `g-${pid}` });
    }
    if (typeof trueDays === "number") {
      dots.push({ value: trueDays, color: "#ffd400", size: 14, key: "true" });
    }
  }

  return (
    React.createElement("div", { className: "range-wrap" },
      React.createElement("div", { className: `range-overlay ${reveal ? "show" : ""}` },
        dots.map((d) => React.createElement("span", { key: d.key, className: "dot", style: { left: `${(d.value / 365) * 100}%`, background: d.color, width: d.size, height: d.size } }))
      ),
      React.createElement("input", {
        type: "range",
        min: 0,
        max: 365,
        step: 1,
        value: hasGuess ? guessDays : 0,
        className: hasGuess ? "has-guess" : "",
        onChange,
        onMouseUp: onCommit,
        onTouchEnd: onCommit,
        disabled,
      }),
      React.createElement("div", { className: "ticks" },
        marks.map((m, index) => React.createElement("span", { 
          key: m, 
          style: { transform: `translateX(${index === marks.length - 1 ? 8 : (index / (marks.length - 1)) * 16}px)` }
        }, `${m}m`))
      ),
      React.createElement("div", { className: "range-label" }, disabled ? "Make a guess when the game starts" : (hasGuess ? `Your guess is: ${label}` : "Pick an age..."))
    )
  );
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(React.createElement(App)); 