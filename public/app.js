(() => {
  "use strict";
  const API = { bookings: "/api/bookings?limit=50", results: "/api/results?limit=50" };
  const $ = (id) => document.getElementById(id);
  const state = { token: sessionStorage.getItem("fitbot_admin_token") || "", bookings: [], results: [] };
  const personName = (id) => id === "person_1" ? "Persona 1" : id === "person_2" ? "Persona 2" : id || "—";
  const statusName = (status) => ({
    scheduled:"Programada", pending_schedule:"Pendiente", scheduling:"Programando",
    dry_run:"Simulación completada", processing:"En proceso",
    booking_confirmed:"Reserva confirmada", already_booked:"Ya reservada",
    booking_failed:"Error de reserva", schedule_failed:"Error al programar",
    outcome_unconfirmed:"Resultado sin confirmar", cancelled:"Cancelada"
  }[status] || status || "Desconocido");
  const dateTime = (value, withTime = true) => {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("es-ES", withTime
      ? { dateStyle:"medium", timeStyle:"short", timeZone:"Europe/Madrid" }
      : { dateStyle:"medium", timeZone:"Europe/Madrid" }).format(date);
  };
  const classDate = (date, time) => {
    if (!date) return "—";
    const parsed = new Date(date + "T" + (time || "00:00") + ":00");
    return new Intl.DateTimeFormat("es-ES",{weekday:"short",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",timeZone:"Europe/Madrid"}).format(parsed);
  };
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const statusClass = (status) => "status-" + String(status || "").replace(/[^a-z_]/g,"");
  let toastTimer;
  function toast(message) {
    const el=$("toast"); el.textContent=message; el.classList.add("show");
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove("show"),3000);
  }
  function showDashboard(visible) {
    $("auth-panel").hidden=visible; $("dashboard").hidden=!visible;
    $("logout-button").hidden=!visible; $("refresh-button").hidden=!visible;
  }
  async function request(path, options={}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Authorization":"Bearer "+state.token, ...(options.headers || {}) },
      cache:"no-store"
    });
    const data = await response.json().catch(()=>({error:"Respuesta no válida del servidor"}));
    if (response.status === 401) {
      state.token=""; sessionStorage.removeItem("fitbot_admin_token"); showDashboard(false);
      throw new Error("Token no válido o sesión caducada. Vuelve a conectarte.");
    }
    if (!response.ok) throw new Error(data.error || "Error HTTP "+response.status);
    return data;
  }
  async function connect(token) {
    state.token=token;
    const data=await request("/api/bookings?limit=1");
    sessionStorage.setItem("fitbot_admin_token",token);
    showDashboard(true);
    await refreshAll();
  }
  function renderStats() {
    const counts = state.bookings.reduce((acc,b)=>{acc[b.status]=(acc[b.status]||0)+1;return acc;},{});
    $("stat-scheduled").textContent=counts.scheduled||counts.pending_schedule||0;
    $("stat-dry-run").textContent=counts.dry_run||0;
    $("stat-errors").textContent=(counts.schedule_failed||0)+(counts.booking_failed||0)+(counts.outcome_unconfirmed||0);
  }
  function renderBookings() {
    const rows=$("booking-rows");
    if (!state.bookings.length) { rows.innerHTML='<tr><td colspan="6" class="empty-state">Todavía no hay reservas programadas.</td></tr>'; return; }
    rows.innerHTML=state.bookings.map(b=>`<tr>
      <td><span class="person-pill">${escapeHtml(personName(b.person_id))}</span></td>
      <td>${escapeHtml(b.name)}</td>
      <td>${escapeHtml(classDate(b.date,b.time))}</td>
      <td>${escapeHtml(dateTime(b.opens_at))}</td>
      <td><span class="status-pill ${statusClass(b.status)}">${escapeHtml(statusName(b.status))}</span></td>
      <td>${["pending_schedule","schedule_failed","scheduling","scheduled"].includes(b.status) ? `<button class="cancel-button" type="button" data-cancel-id="${escapeHtml(b.id)}">Cancelar</button>` : "—"}</td>
    </tr>`).join("");
  }
  function renderActivity() {
    const entries=[];
    state.results.slice(0,8).forEach(r=>entries.push({
      title:r.message || r.detail || statusName(r.status || r.result),
      sub:[personName(r.person_id),r.activity || r.name,r.date && classDate(r.date,r.time)].filter(Boolean).join(" · "),
      time:r.recorded_at || r.updated_at || r.created_at,
      status:r.status || r.result
    }));
    if (!entries.length) state.bookings.slice(0,8).forEach(b=>entries.push({
      title:statusName(b.status)+" · "+(b.name||"Reserva"),
      sub:personName(b.person_id)+" · "+classDate(b.date,b.time),
      time:b.updated_at||b.created_at,status:b.status
    }));
    if (!entries.length) { $("activity-list").innerHTML='<div class="empty-state">Aún no hay actividad registrada.</div>'; return; }
    $("activity-list").innerHTML=entries.map(e=>`<div class="activity-item">
      <div class="activity-icon">${["dry_run","booking_confirmed","already_booked"].includes(e.status)?"✓":String(e.status||"").includes("failed")?"!":"↗"}</div>
      <div class="activity-copy"><strong>${escapeHtml(e.title)}</strong><small>${escapeHtml(e.sub||"FitBot")}</small></div>
      <time class="activity-time">${escapeHtml(dateTime(e.time))}</time>
    </div>`).join("");
  }
  async function refreshAll(initialBookings) {
    const [bookingData,resultData]=await Promise.all([
      initialBookings ? Promise.resolve(initialBookings) : request(API.bookings),
      request(API.results).catch(()=>({results:[]}))
    ]);
    state.bookings=bookingData.bookings||[];
    state.results=resultData.results||[];
    renderStats(); renderBookings(); renderActivity();
    $("last-updated").textContent="Actualizado "+new Intl.DateTimeFormat("es-ES",{hour:"2-digit",minute:"2-digit"}).format(new Date());
  }
  $("auth-form").addEventListener("submit",async(event)=>{
    event.preventDefault(); const button=event.submitter; button.disabled=true; $("auth-error").textContent="";
    try { await connect($("admin-token").value.trim()); toast("Conectado a FitBot"); }
    catch(error){$("auth-error").textContent=error.message; state.token="";sessionStorage.removeItem("fitbot_admin_token");}
    finally{button.disabled=false;}
  });
  $("logout-button").addEventListener("click",()=>{
    state.token="";sessionStorage.removeItem("fitbot_admin_token");$("admin-token").value="";showDashboard(false);
  });
  async function refresh() {
    $("refresh-button").disabled=true;
    try { await refreshAll(); toast("Datos actualizados"); }
    catch(error){toast(error.message);}
    finally{$("refresh-button").disabled=false;}
  }
  $("refresh-button").addEventListener("click",refresh);
  $("refresh-inline").addEventListener("click",refresh);
  $("booking-rows").addEventListener("click",async(event)=>{
    const button=event.target.closest("[data-cancel-id]");
    if(!button) return;
    const bookingId=button.dataset.cancelId;
    if(!confirm("¿Cancelar esta programación? Si QStash ya tiene el aviso, el webhook lo ignorará.")) return;
    button.disabled=true;
    try {
      await request("/api/bookings?id="+encodeURIComponent(bookingId),{method:"DELETE"});
      toast("Programación cancelada");
      await refreshAll();
    } catch(error) {
      toast(error.message);
      button.disabled=false;
    }
  });
  $("booking-form").addEventListener("submit",async(event)=>{
    event.preventDefault();
    const button=$("schedule-button"), error=$("booking-error"), success=$("booking-success");
    error.textContent="";success.textContent="";button.disabled=true;
    const payload={
      person_id:$("person").value,
      date:$("class-date").value,
      time:$("class-time").value,
      name:$("activity").value
    };
    try {
      const key="fitbot-ui-"+crypto.randomUUID();
      const data=await request("/api/bookings",{
        method:"POST",
        headers:{"Content-Type":"application/json","Idempotency-Key":key},
        body:JSON.stringify(payload)
      });
      success.textContent="Reserva programada. Apertura: "+dateTime(data.booking?.opens_at)+". Estado: "+statusName(data.status)+".";
      toast("Automatización creada");
      await refreshAll();
    } catch(err) { error.textContent=err.message; }
    finally {button.disabled=false;}
  });
  const now=new Date(); $("class-date").min=new Date(now.getTime()-now.getTimezoneOffset()*60000).toISOString().slice(0,10);
  if (state.token) {
    connect(state.token).catch((error)=>{showDashboard(false);$("auth-error").textContent=error.message;});
  } else showDashboard(false);
})();