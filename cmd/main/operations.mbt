///|
struct RequestTrace {
  id : Int64
  mut status : Int32
  mut ended : Bool
  mut close_after : Bool
}

///|
struct LiveConnection {
  raw : @http.ServerConnection
  mut active : Bool
  mut task : @async.Task[Unit]?
}

///|
struct Runtime {
  started : Int64
  mut draining : Bool
  mut stopped : Bool
  mut failed : Bool
  mut sequence : Int64
  mut active : Int32
  mut completed : Int64
  mut errors : Int64
  mut duration_ms : Int64
  mut buffered_bytes : Int32
  statuses : Map[Int32, Int64]
  connections : Map[Int64, LiveConnection]
}

///|
fn Runtime::new() -> Runtime {
  {
    started: @async.now(),
    draining: false,
    stopped: false,
    failed: false,
    sequence: 0L,
    active: 0,
    completed: 0L,
    errors: 0L,
    duration_ms: 0L,
    buffered_bytes: 0,
    statuses: Map([]),
    connections: Map([]),
  }
}

///|
fn log_record(
  level : String,
  event : String,
  fields : Map[String, Json],
) -> Unit {
  fields["timestamp_ms"] = Json::number(@env.now().to_double())
  fields["level"] = Json::string(level)
  fields["event"] = Json::string(event)
  println(Json::object(fields).stringify())
  flush_logs()
}

///|
extern "C" fn flush_logs() -> Unit = "moon_s3_flush_logs"

///|
fn log_event(level : String, event : String, message : String) -> Unit {
  log_record(level, event, { "message": Json::string(message) })
}

///|
fn metrics(runtime : Runtime) -> String {
  let mut text = "# TYPE moon_s3_uptime_seconds gauge\nmoon_s3_uptime_seconds \{(@async.now() - runtime.started).to_double() / 1000.0}\n" +
    "# TYPE moon_s3_draining gauge\nmoon_s3_draining \{if runtime.draining { 1 } else { 0 }}\n" +
    "# TYPE moon_s3_connections gauge\nmoon_s3_connections \{runtime.connections.length()}\n" +
    "# TYPE moon_s3_requests_active gauge\nmoon_s3_requests_active \{runtime.active}\n" +
    "# TYPE moon_s3_buffered_request_bytes gauge\nmoon_s3_buffered_request_bytes \{runtime.buffered_bytes}\n" +
    "# TYPE moon_s3_requests_total counter\n"
  let statuses = runtime.statuses.keys().to_array()
  statuses.sort()
  for status in statuses {
    text += "moon_s3_requests_total{status=\"\{status}\"} \{runtime.statuses.get(status).unwrap_or(0L)}\n"
  }
  text += "# TYPE moon_s3_request_failures_total counter\nmoon_s3_request_failures_total \{runtime.errors}\n" +
    "# TYPE moon_s3_request_duration_seconds summary\nmoon_s3_request_duration_seconds_sum \{runtime.duration_ms.to_double() / 1000.0}\nmoon_s3_request_duration_seconds_count \{runtime.completed}\n"
  text
}

///|
async fn serve_connection(config : Config, connection : LiveConnection) -> Unit {
  let runtime = config.runtime
  defer connection.raw.close()
  while !runtime.draining {
    let request = match
      @async.with_timeout_opt(config.request_timeout, () => {
        connection.raw.read_request()
      }) {
      Some(request) => request
      None => return
    }
    if runtime.draining {
      break
    }
    connection.active = true
    runtime.active += 1
    runtime.sequence += 1L
    let trace : RequestTrace = {
      id: runtime.sequence,
      status: 0,
      ended: false,
      close_after: false,
    }
    let started = @async.now()
    defer {
      connection.active = false
      runtime.active -= 1
      runtime.completed += 1L
      runtime.duration_ms += @async.now() - started
      runtime.statuses[trace.status] = runtime.statuses
        .get(trace.status)
        .unwrap_or(0L) +
        1L
      log_record("info", "request_completed", {
        "request_id": Json::string(trace.id.to_string()),
        "method": Json::string(method_name(request.meth)),
        "status": Json::number(trace.status.to_double()),
        "duration_ms": Json::number((@async.now() - started).to_double()),
        "cancelled": Json::boolean(@async.is_being_cancelled()),
      })
    }
    try {
      let finished = @async.with_timeout_opt(config.request_timeout, () => {
        handle_request(config, request, connection.raw, connection.raw, trace)
        if !trace.close_after {
          if !trace.ended {
            connection.raw.end_response()
          }
          connection.raw.skip_request_body()
        }
      })
      if finished is None {
        runtime.errors += 1L
        log_event(
          "warn", "request_timeout", "Request deadline exceeded; connection closed.",
        )
        return
      }
      if trace.close_after {
        return
      }
    } catch {
      _ => {
        runtime.errors += 1L
        log_event(
          "error", "request_failed", "Request failed; connection closed.",
        )
        return
      }
    }
  }
}

///|
async fn run_service(config : Config, server : @http.Server) -> Unit {
  let runtime = config.runtime
  defer {
    server.close()
    runtime.stopped = true
  }
  @async.with_task_group(group => {
    let webhook = if config.webhook_url is Some(_) {
      Some(group.spawn(() => dispatch_events(config), no_wait=true))
    } else {
      None
    }
    let acceptor = group.spawn(no_wait=true, () => {
      let mut connection_id = 0L
      while !runtime.draining {
        let (raw, _) = server.accept()
        if runtime.connections.length() >= config.max_connections {
          raw.close()
          continue
        }
        if runtime.draining {
          raw.close()
          break
        }
        connection_id += 1L
        let id = connection_id
        let connection : LiveConnection = { raw, active: false, task: None, }
        runtime.connections[id] = connection
        connection.task = Some(
          group.spawn(no_wait=true, () => {
            defer {
              connection.task = None
              runtime.connections.remove(id) |> ignore
            }
            serve_connection(config, connection) catch {
              _ => ()
            }
          }),
        )
      }
    })
    while !runtime.draining {
      @async.sleep(10)
    }
    acceptor.cancel()
    if webhook is Some(task) {
      task.cancel()
    }
    for _, connection in runtime.connections {
      if !connection.active {
        if connection.task is Some(task) {
          task.cancel()
        }
      }
    }
    let drained = @async.with_timeout_opt(config.shutdown_timeout, () => {
      while runtime.active > 0 {
        @async.sleep(10)
      }
    })
    log_event(
      if drained is Some(_) {
        "info"
      } else {
        "warn"
      },
      if drained is Some(_) {
        "shutdown_drained"
      } else {
        "shutdown_timeout"
      },
      "Shutdown drain finished; remaining requests: \{runtime.active}.",
    )
  })
}

///|
async fn serve(config : Config, server : @http.Server) -> Unit {
  let runtime = config.runtime
  @async.handle_cancellation(() => {
    @async.with_task_group(group => {
      group.spawn_bg(no_wait=true, () => {
        @async.protect_from_cancel(() => run_service(config, server)) catch {
          _ => {
            runtime.failed = true
            log_event(
              "error", "server_failed", "Server terminated unexpectedly.",
            )
          }
        }
      })
      @async.handle_cancellation(() => {
        while !runtime.stopped {
          @async.sleep(100)
        }
      })
      |> ignore
      runtime.draining = true
      server.close()
      log_event(
        "info", "shutdown_started", "Stopped accepting connections; draining active requests.",
      )
    })
  })
  |> ignore
  log_event(
    "info", "server_stopped", "Server stopped; storage lock will be released.",
  )
  @env.exit(if runtime.failed { 1 } else { 0 })
}
