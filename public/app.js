(() => {
  "use strict";
  const API = { bookings: "/api/bookings?limit=50", results: "/api/results?limit=50" };
  const $ = (id) => document.getElementById(id);
  const state = { token: sessionStorage.getItem("fitbot_admin_token") || "", bookings: [], results: [], editingBookingId: null, editingIdempotencyKey: null, editingFingerprint: null, historyPerson: "all", historyStatus: "all", historyFrom: "", historyTo: "" };
  const personName = (id) => id === "person_1" ? "Joan" : id === "person_2" ? "Paula" : id || "—";
  const statusName = (status) => ({
    scheduled:"Programada", pending_schedule:"Pendiente", scheduling:"Programando",
    dry_run:"Simulación completada", processing:"En proceso",
    booking_confirmed:"Reserva confirmada", already_booked:"Ya reservada",
    booking_failed:"Error de reserva", schedule_failed:"Error al programar",
    outcome_unconfirmed:"Resultado sin confirmar", cancelled:"Cancelada", replaced:"Sustituida"
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
  const completedStatuses = new Set(["dry_run","booking_confirmed","already_booked"]);
  const incidentStatuses = new Set(["booking_failed","schedule_failed","outcome_unconfirmed","incorrect_credentials","class_not_found"]);
  const pendingStatuses = new Set(["scheduled","pending_schedule","scheduling","processing"]);
  function historyCategory(status) {
    if (completedStatuses.has(status)) return "completed";
    if (status === "cancelled") return "cancelled";
    if (pendingStatuses.has(status)) return "pending";
    return "incident";
  }
  function historyRecords() {
    const resultById = new Map();
    state.results.forEach(result => {
      if (result.booking_id && !resultById.has(result.booking_id)) resultById.set(result.booking_id,result);
    });
    const records = state.bookings.filter(b=>b.status!=="replaced").map(b=>{
      const result=resultById.get(b.id);
      return {...b,...(result||{}),id:b.id,status:result?.status||b.status,name:b.name||result?.class||result?.name||"Reserva",date:b.date||result?.date,time:b.time||result?.time,person_id:b.person_id||result?.person_id,recorded_at:result?.recorded_at||b.updated_at||b.created_at};
    });
    const knownIds = new Set(records.map(item=>item.id));
    state.results.filter(r=>!r.booking_id || !knownIds.has(r.booking_id)).forEach((r,index)=>{
      records.push({...r,id:r.booking_id||("legacy-"+(r.recorded_at||index)),name:r.class||r.name||"Reserva",status:r.status||r.result});
    });
    return records.sort((a,b)=>String((b.date||"")+"T"+(b.time||"")).localeCompare(String((a.date||"")+"T"+(a.time||""))));
  }
  function renderBookings() {
    const rows=$("booking-rows");
    const all=historyRecords();
    const filtered=all.filter(b=>{
      if(state.historyPerson!=="all" && b.person_id!==state.historyPerson) return false;
      const category=historyCategory(b.status);
      if(state.historyStatus!=="all" && category!==state.historyStatus) return false;
      const date=b.date||"";
      if(state.historyFrom && (!date || date<state.historyFrom)) return false;
      if(state.historyTo && (!date || date>state.historyTo)) return false;
      return true;
    });
    $("history-count").textContent="Mostrando "+filtered.length+" de "+all.length;
    if (!filtered.length) { rows.innerHTML='<tr><td colspan="6" class="empty-state">No hay reservas que coincidan con estos filtros.</td></tr>'; return; }
    rows.innerHTML=filtered.map(b=>`<tr>
      <td><span class="person-pill">${escapeHtml(personName(b.person_id))}</span></td>
      <td>${escapeHtml(b.name)}</td>
      <td>${escapeHtml(classDate(b.date,b.time))}</td>
      <td><span class="status-pill ${statusClass(b.status)}">${escapeHtml(statusName(b.status))}</span></td>
      <td>${escapeHtml(dateTime(b.recorded_at||b.updated_at||b.created_at))}</td>
      <td>${b.id && ["pending_schedule","schedule_failed","scheduling","scheduled"].includes(b.status) ? `<div class="row-actions"><button class="edit-button" type="button" data-edit-id="${escapeHtml(b.id)}">Editar</button><button class="cancel-button" type="button" data-cancel-id="${escapeHtml(b.id)}">Cancelar</button></div>` : "—"}</td>
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
    if (!entries.length) state.bookings.filter(b=>b.status!=="replaced").slice(0,8).forEach(b=>entries.push({
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
  $("history-person").addEventListener("change",event=>{state.historyPerson=event.target.value;renderBookings();});
  $("history-status").addEventListener("change",event=>{state.historyStatus=event.target.value;renderBookings();});
  $("history-from").addEventListener("change",event=>{state.historyFrom=event.target.value;renderBookings();});
  $("history-to").addEventListener("change",event=>{state.historyTo=event.target.value;renderBookings();});
  $("history-clear").addEventListener("click",()=>{
    state.historyPerson="all";state.historyStatus="all";state.historyFrom="";state.historyTo="";
    $("history-person").value="all";$("history-status").value="all";$("history-from").value="";$("history-to").value="";
    renderBookings();
  });
  async function cancelBooking(button, bookingId) {
    try {
      await request("/api/bookings?id="+encodeURIComponent(bookingId),{method:"DELETE"});
      const booking=state.bookings.find(item=>item.id===bookingId);
      if(booking) booking.status="cancelled";
      renderStats();
      renderBookings();
      renderActivity();
      toast("Programación cancelada");
      refreshAll().catch(()=>toast("Cancelada, pero no se pudo actualizar el historial. Pulsa Actualizar."));
    } catch(error) {
      button.disabled=false;
      button.removeAttribute("aria-busy");
      button.textContent="Cancelar";
      toast(error.message);
    }
  }
  $("booking-rows").addEventListener("click",(event)=>{
    const editButton=event.target.closest("[data-edit-id]");
    if(editButton){
      const booking=state.bookings.find(item=>item.id===editButton.dataset.editId);
      if(!booking || editButton.disabled) return;
      state.editingBookingId=booking.id;
      state.editingIdempotencyKey=null;
      state.editingFingerprint=null;
      $("person").value=booking.person_id;
      $("activity").value=booking.name;
      $("class-date").value=booking.date;
      $("class-time").value=booking.time;
      $("schedule-button").textContent="Guardar cambios →";
      $("stop-editing").hidden=false;
      $("booking-error").textContent="";
      $("booking-success").textContent="Editando la programación seleccionada. La anterior quedará como sustituida al guardar."; 
      $("booking-form").scrollIntoView({behavior:"smooth",block:"center"});
      return;
    }
    const button=event.target.closest("[data-cancel-id]");
    if(!button || button.disabled) return;
    const bookingId=button.dataset.cancelId;
    if(!confirm("¿Cancelar esta programación? Si QStash ya tiene el aviso, el webhook lo ignorará.")) return;
    button.disabled=true;
    button.textContent="Cancelando…";
    button.setAttribute("aria-busy","true");
    void cancelBooking(button,bookingId);
  });
  $("stop-editing").addEventListener("click",()=>{
    state.editingBookingId=null;
    state.editingIdempotencyKey=null;
    state.editingFingerprint=null;
    $("booking-form").reset();
    $("schedule-button").textContent="Programar reserva →";
    $("stop-editing").hidden=true;
    $("booking-error").textContent="";
    $("booking-success").textContent="";
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
      const editingId=state.editingBookingId;
      const fingerprint=JSON.stringify(payload);
      if(editingId && state.editingFingerprint!==fingerprint){
        state.editingFingerprint=fingerprint;
        state.editingIdempotencyKey="fitbot-ui-"+crypto.randomUUID();
      }
      const key=editingId ? state.editingIdempotencyKey : "fitbot-ui-"+crypto.randomUUID();
      const body=editingId ? {id:editingId,...payload} : payload;
      const data=await request("/api/bookings",{
        method:editingId?"PATCH":"POST",
        headers:{"Content-Type":"application/json","Idempotency-Key":key},
        body:JSON.stringify(body)
      });
      success.textContent=(editingId?"Reserva modificada. ":"Reserva programada. ")+"Apertura: "+dateTime(data.booking?.opens_at)+". Estado: "+statusName(data.status)+"."; 
      state.editingBookingId=null;
      state.editingIdempotencyKey=null;
      state.editingFingerprint=null;
      button.textContent="Programar reserva →";
      $("stop-editing").hidden=true;
      toast(editingId?"Cambios guardados":"Automatización creada");
      await refreshAll();
    } catch(err) { error.textContent=err.message; }
    finally {button.disabled=false;}
  });
  const timeSelect=$("class-time");
  for(let minutes=7*60;minutes<=20*60+15;minutes+=15){
    const hour=String(Math.floor(minutes/60)).padStart(2,"0");
    const minute=String(minutes%60).padStart(2,"0");
    const value=hour+":"+minute;
    const option=document.createElement("option");
    option.value=value;
    option.textContent=value;
    timeSelect.append(option);
  }
  const now=new Date(); $("class-date").min=new Date(now.getTime()-now.getTimezoneOffset()*60000).toISOString().slice(0,10);
  if (state.token) {
    connect(state.token).catch((error)=>{showDashboard(false);$("auth-error").textContent=error.message;});
  } else showDashboard(false);
})();