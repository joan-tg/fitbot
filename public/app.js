(() => {
  "use strict";
  const API = { bookings: "/api/bookings?limit=100", results: "/api/results?limit=100" };
  const $ = (id) => document.getElementById(id);
  const state = { token: sessionStorage.getItem("fitbot_admin_token") || "", bookings: [], results: [], editingBookingId: null, editingIdempotencyKey: null, editingFingerprint: null, historyPerson: "all", historyStatus: "all", historyFrom: "", historyTo: "", calendarYear: new Date().getFullYear(), calendarMonth: new Date().getMonth(), selectedCalendarDate: null };
  const personName = (id) => id === "person_1" ? "Joan" : id === "person_2" ? "Paula" : id || "—";
  const statusName = (status) => ({
    scheduled:"Programada", pending_schedule:"Pendiente", scheduling:"Programando",
    dry_run:"Simulación completada", processing:"En proceso",
    booking_confirmed:"Reserva confirmada", already_booked:"Ya reservada",
    booking_failed:"Error de reserva", schedule_failed:"Error al programar",
    outcome_unconfirmed:"Resultado sin confirmar", booking_missed:"Ventana caducada", cancelled:"Cancelada", replaced:"Sustituida"
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
    $("stat-errors").textContent=(counts.schedule_failed||0)+(counts.booking_failed||0)+(counts.outcome_unconfirmed||0)+(counts.booking_missed||0);
  }
  const quotaPolicy = {
    countedStatuses: new Set(["scheduled","processing","booking_confirmed","already_booked"]),
    uncertainStatuses: new Set(["outcome_unconfirmed"]),
    members: [
      {id:"person_1",name:"Joan",classes:17,openBox:4},
      {id:"person_2",name:"Paula",classes:13,openBox:4}
    ]
  };
  function madridMonthKey(date=new Date()) {
    const parts=new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Madrid",year:"numeric",month:"2-digit"}).formatToParts(date);
    const year=parts.find(part=>part.type==="year").value;
    const month=parts.find(part=>part.type==="month").value;
    return year+"-"+month;
  }
  function quotaActivityType(record) {
    if(record.activity_type==="open_box" || record.activity_type==="class") return record.activity_type;
    const activity=String(record.name||record.class||"").trim().toLocaleLowerCase("es");
    return activity==="open box" ? "open_box" : "class";
  }
  function renderQuotaWarnings() {
    const monthKey=madridMonthKey();
    const monthDate=new Date(monthKey+"-01T12:00:00Z");
    $("quota-month").textContent=new Intl.DateTimeFormat("es-ES",{month:"long",year:"numeric",timeZone:"Europe/Madrid"}).format(monthDate);
    const records=historyRecords().filter(record=>record.date && record.date.slice(0,7)===monthKey);
    const summaries=quotaPolicy.members.map(member=>{
      const personRecords=records.filter(record=>record.person_id===member.id);
      const count=(type)=>personRecords.filter(record=>
        quotaActivityType(record)===type && quotaPolicy.countedStatuses.has(record.status)
      ).length;
      const uncertain=personRecords.filter(record=>quotaPolicy.uncertainStatuses.has(record.status)).length;
      return {
        ...member,
        classesUsed:count("class"),
        openBoxUsed:count("open_box"),
        uncertain
      };
    });
    const renderQuota=(label,used,limit)=>{
      const exceeded=used>limit;
      const reached=used===limit;
      const status=exceeded
        ? "Has superado la cuota: "+used+"/"+limit
        : reached ? "Cuota alcanzada: "+used+"/"+limit : used+" de "+limit+" utilizadas";
      return '<div class="quota-line'+(reached||exceeded?' quota-warning':'')+(exceeded?' quota-exceeded':'')+'"><span>'+label+'</span><strong>'+status+'</strong></div>';
    };
    $("quota-summary").innerHTML=summaries.map(member=>{
      const alerts=[];
      if(member.uncertain) alerts.push(member.uncertain+" resultado"+(member.uncertain===1?" incierto":"s inciertos")+" sin confirmar; no se incluye"+(member.uncertain===1?"":"n")+" en el cómputo.");
      return '<article class="quota-person"><h3>'+member.name+'</h3>'+
        renderQuota("Clases",member.classesUsed,member.classes)+
        renderQuota("Open Box",member.openBoxUsed,member.openBox)+
        (alerts.length?'<p class="quota-uncertain">'+alerts.join(" ")+' Conviene revisarlo antes de asumir que no consume cuota.</p>':'')+
        '</article>';
    }).join("");
  }
  const completedStatuses = new Set(["dry_run","booking_confirmed","already_booked"]);
  const incidentStatuses = new Set(["booking_failed","schedule_failed","outcome_unconfirmed","incorrect_credentials","class_not_found","booking_missed"]);
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
    const knownIds = new Set(state.bookings.map(booking=>booking.id));
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
      <td><span class="status-pill ${statusClass(b.status)}">${escapeHtml(statusName(b.status))}</span>${b.status==="booking_missed"?'<small class="status-detail">La tarea llegó después de los 30 minutos disponibles.</small>':""}</td>
      <td>${escapeHtml(dateTime(b.recorded_at||b.updated_at||b.created_at))}</td>
      <td>${b.id && ["pending_schedule","schedule_failed","scheduling","scheduled"].includes(b.status) ? `<div class="row-actions"><button class="edit-button" type="button" data-edit-id="${escapeHtml(b.id)}">Editar</button><button class="cancel-button" type="button" data-cancel-id="${escapeHtml(b.id)}">Cancelar</button></div>` : "—"}</td>
    </tr>`).join("");
  }
  function calendarCategory(status) {
    if (completedStatuses.has(status)) return "completed";
    if (status === "cancelled") return "cancelled";
    if (pendingStatuses.has(status)) return "pending";
    return "incident";
  }
  function calendarMonthKey(year, month) {
    return year+"-"+String(month+1).padStart(2,"0");
  }
  function calendarDateKey(year, month, day) {
    return calendarMonthKey(year,month)+"-"+String(day).padStart(2,"0");
  }
  function renderCalendar() {
    const year=state.calendarYear, month=state.calendarMonth;
    const first=new Date(year,month,1);
    const daysInMonth=new Date(year,month+1,0).getDate();
    const mondayOffset=(first.getDay()+6)%7;
    const totalCells=Math.ceil((mondayOffset+daysInMonth)/7)*7;
    const monthKey=calendarMonthKey(year,month);
    const monthLabel=new Intl.DateTimeFormat("es-ES",{month:"long",year:"numeric"}).format(first);
    $("calendar-month-title").textContent=monthLabel.charAt(0).toLocaleUpperCase("es-ES")+monthLabel.slice(1);
    const monthRecords=historyRecords().filter(item=>item.date && item.date.slice(0,7)===monthKey && item.status!=="replaced");
    const byDate=new Map();
    monthRecords.forEach(item=>{
      if(!byDate.has(item.date)) byDate.set(item.date,[]);
      byDate.get(item.date).push(item);
    });
    const today=new Date();
    const todayKey=calendarDateKey(today.getFullYear(),today.getMonth(),today.getDate());
    if(!state.selectedCalendarDate || state.selectedCalendarDate.slice(0,7)!==monthKey) {
      state.selectedCalendarDate=monthKey===calendarMonthKey(today.getFullYear(),today.getMonth())?todayKey:calendarDateKey(year,month,1);
    }
    const cells=[];
    for(let index=0;index<totalCells;index++){
      const day=index-mondayOffset+1;
      const inMonth=day>=1 && day<=daysInMonth;
      const dateKey=inMonth?calendarDateKey(year,month,day):"";
      const events=inMonth?(byDate.get(dateKey)||[]):[];
      const count=events.length;
      const classes=["calendar-day"];
      if(!inMonth) classes.push("is-outside");
      if(dateKey===todayKey) classes.push("is-today");
      if(dateKey===state.selectedCalendarDate) classes.push("is-selected");
      const preview=events.slice(0,2).map(item=>'<span class="calendar-event '+calendarCategory(item.status)+'">'+escapeHtml(item.time||"—")+' · '+escapeHtml(personName(item.person_id))+'</span>').join("");
      const more=count>2?'<span class="calendar-more">+'+(count-2)+' más</span>':"";
      cells.push('<button class="'+classes.join(" ")+'" type="button" data-calendar-date="'+dateKey+'" '+(!inMonth?'disabled':'')+' aria-label="'+(inMonth?escapeHtml(day+" de "+monthLabel+", "+count+" clases"):"")+'" aria-pressed="'+(dateKey===state.selectedCalendarDate)+'"><span class="calendar-day-number">'+(inMonth?day:"")+'</span>'+preview+more+'</button>');
    }
    $("calendar-grid").innerHTML=cells.join("");
    const selected=(byDate.get(state.selectedCalendarDate)||[]).sort((a,b)=>String(a.time||"").localeCompare(String(b.time||"")));
    const selectedDate=state.selectedCalendarDate?new Date(state.selectedCalendarDate+"T12:00:00") : first;
    $("calendar-selected-title").textContent=new Intl.DateTimeFormat("es-ES",{weekday:"long",day:"numeric",month:"long"}).format(selectedDate);
    $("calendar-day-count").textContent=selected.length?selected.length+" clase"+(selected.length===1?"":"s"):"Sin clases";
    if(!selected.length) {
      $("calendar-day-events").innerHTML='<div class="empty-state">No hay clases registradas para este día.</div>';
      return;
    }
    $("calendar-day-events").innerHTML=selected.map(item=>'<article class="calendar-event-card"><div class="calendar-event-time">'+escapeHtml(item.time||"—")+'</div><div class="calendar-event-copy"><strong>'+escapeHtml(item.name||"Clase")+'</strong><small>'+escapeHtml(personName(item.person_id))+'</small>'+(item.status==="booking_missed"?'<small class="status-detail">Llegó fuera de la ventana de 30 min.</small>':"")+'</div><span class="status-pill '+statusClass(item.status)+'">'+escapeHtml(statusName(item.status))+'</span></article>').join("");
  }
  function setCalendarMonth(year,month) {
    const date=new Date(year,month,1);
    state.calendarYear=date.getFullYear();state.calendarMonth=date.getMonth();
    state.selectedCalendarDate=null;
    renderCalendar();
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
      <div class="activity-icon">${["dry_run","booking_confirmed","already_booked"].includes(e.status)?"✓":incidentStatuses.has(e.status)?"!":"↗"}</div>
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
    renderStats(); renderQuotaWarnings(); renderBookings(); renderActivity(); renderCalendar();
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
  $("show-calendar").addEventListener("click",()=>{
    $("calendar-panel").hidden=false;$("history-panel").hidden=true;
    $("show-calendar").classList.add("is-active");$("show-history").classList.remove("is-active");
    $("show-calendar").setAttribute("aria-pressed","true");$("show-history").setAttribute("aria-pressed","false");
    renderCalendar();
  });
  $("show-history").addEventListener("click",()=>{
    $("calendar-panel").hidden=true;$("history-panel").hidden=false;
    $("show-history").classList.add("is-active");$("show-calendar").classList.remove("is-active");
    $("show-history").setAttribute("aria-pressed","true");$("show-calendar").setAttribute("aria-pressed","false");
  });
  $("calendar-prev").addEventListener("click",()=>setCalendarMonth(state.calendarYear,state.calendarMonth-1));
  $("calendar-next").addEventListener("click",()=>setCalendarMonth(state.calendarYear,state.calendarMonth+1));
  $("calendar-today").addEventListener("click",()=>setCalendarMonth(new Date().getFullYear(),new Date().getMonth()));
  $("calendar-grid").addEventListener("click",event=>{
    const day=event.target.closest("[data-calendar-date]");
    if(!day || !day.dataset.calendarDate) return;
    state.selectedCalendarDate=day.dataset.calendarDate;
    renderCalendar();
  });
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