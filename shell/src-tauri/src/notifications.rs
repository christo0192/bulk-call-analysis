use serde_json::Value;
use std::{
    collections::HashMap,
    time::{Duration, SystemTime},
};

#[derive(Debug, Default)]
pub struct NotifyCtx {
    pub project_titles: HashMap<String, String>,
    pub progress: HashMap<String, (u64, u64)>,
    pub active_job: Option<String>,
    pub pause_reason: Option<String>,
    pub previous_power: Option<crate::power::PowerStatus>,
    pub now: SystemTime,
    seen: HashMap<(String, String), SystemTime>,
}

fn params(event: &Value) -> &Value {
    event.get("params").unwrap_or(event)
}

pub fn observe(event: &Value, ctx: &mut NotifyCtx) {
    let p = params(event);
    match event.get("method").and_then(Value::as_str) {
        Some("job/progress") => {
            if let Some(job) = p.get("job").and_then(Value::as_str) {
                ctx.progress.insert(
                    job.to_owned(),
                    (
                        p.get("done").and_then(Value::as_u64).unwrap_or(0),
                        p.get("total").and_then(Value::as_u64).unwrap_or(0),
                    ),
                );
            }
        }
        Some("job/state") => {
            if let Some(job) = p.get("job").and_then(Value::as_str) {
                let state = p.get("state").and_then(Value::as_str).unwrap_or("");
                if matches!(state, "running" | "starting" | "active" | "in_progress") {
                    ctx.active_job = Some(job.to_owned());
                } else if matches!(state, "done" | "done_with_issues" | "failed" | "cancelled")
                    && ctx.active_job.as_deref() == Some(job)
                {
                    ctx.active_job = None;
                }
            }
        }
        Some("engine/event") if p.get("kind").and_then(Value::as_str) == Some("pause") => {
            ctx.pause_reason = p.get("reason").and_then(Value::as_str).map(str::to_owned);
        }
        _ => {}
    }
}

fn pause_reason(reason: &str) -> Option<&'static str> {
    match reason {
        "quota" | "usage_limit" => Some("usage limit"),
        "sleep" => Some("computer asleep"),
        "operator" => Some("paused by you"),
        "battery" => Some("on battery"),
        "credit" | "credits" | "spend" | "spend_control" => Some("avoid paid credits"),
        "model_unavailable" => Some("model unavailable"),
        _ if !reason.is_empty() => Some("paused"),
        _ => None,
    }
}

fn relative_resume(now: SystemTime, value: Option<&Value>) -> String {
    let Some(seconds) = value.and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|n| n as i64)))
    else {
        return "Resumes on its own when the allowance resets.".into();
    };
    let target = SystemTime::UNIX_EPOCH + Duration::from_secs(seconds.max(0) as u64);
    let minutes = target
        .duration_since(now)
        .unwrap_or_default()
        .as_secs()
        .div_ceil(60);
    format!(
        "Resumes on its own in about {} h {} min",
        minutes / 60,
        minutes % 60
    )
}

pub fn notification_for(event: &Value, ctx: &NotifyCtx) -> Option<(String, String)> {
    let method = event.get("method").and_then(Value::as_str)?;
    let p = params(event);
    match method {
        "job/state" => {
            let state = p
                .get("state")
                .or_else(|| p.get("status"))
                .and_then(Value::as_str)?;
            let project = p.get("project").and_then(Value::as_str).unwrap_or("");
            let title = ctx
                .project_titles
                .get(project)
                .map(String::as_str)
                .unwrap_or("A job");
            match state {
                "sample_ready" => Some((
                    "Sample ready to check".into(),
                    format!("{title}: the sample is ready to check."),
                )),
                "done" => Some(("Job finished".into(), title.into())),
                "done_with_issues" => {
                    Some(("Job finished with issues to check".into(), title.into()))
                }
                "stalled" => Some((
                    "Job stalled".into(),
                    "No reply from the reader for a few minutes.".into(),
                )),
                _ => None,
            }
        }
        "engine/event" if p.get("kind").and_then(Value::as_str) == Some("pause") => {
            let reason = pause_reason(p.get("reason").and_then(Value::as_str).unwrap_or(""))?;
            let body = if reason == "usage limit" {
                relative_resume(ctx.now, p.get("resume_at").or_else(|| p.get("resumeAt")))
            } else if reason == "avoid paid credits" {
                "Check that automatic top-up is off, then resume.".into()
            } else {
                format!("Paused ({reason}).")
            };
            Some((format!("Paused ({reason})"), body))
        }
        _ => None,
    }
}

pub fn battery_notification(ctx: &NotifyCtx, percent: Option<u8>) -> Option<(String, String)> {
    ctx.active_job.as_ref()?;
    Some((
        "On battery".into(),
        format!(
            "On battery: {}% left. Plug in to keep the job going.",
            percent.unwrap_or(0)
        ),
    ))
}

pub fn should_notify(ctx: &mut NotifyCtx, event: &Value) -> bool {
    let p = params(event);
    let key = (
        p.get("job")
            .or_else(|| p.get("project"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        p.get("state")
            .or_else(|| p.get("kind"))
            .and_then(Value::as_str)
            .unwrap_or_else(|| event.get("method").and_then(Value::as_str).unwrap_or(""))
            .to_owned(),
    );
    if ctx.seen.get(&key).is_some_and(|old| {
        ctx.now.duration_since(*old).unwrap_or_default() < Duration::from_secs(600)
    }) {
        return false;
    }
    ctx.seen.insert(key, ctx.now);
    true
}

pub fn dispatch_notification<F, E>(
    ctx: &mut NotifyCtx,
    event: &Value,
    now: SystemTime,
    send: F,
) -> Result<bool, E>
where
    F: FnOnce(String, String) -> Result<(), E>,
{
    ctx.now = now;
    let Some((title, body)) = notification_for(event, ctx) else {
        return Ok(false);
    };
    if !should_notify(ctx, event) {
        return Ok(false);
    }
    send(title, body)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn real_sample_payload_has_no_invented_count() {
        let mut c = NotifyCtx::default();
        c.project_titles
            .insert("project-1".into(), "Project".into());
        assert_eq!(notification_for(&json!({"method":"job/state","params":{"job":"job-1","project":"project-1","state":"sample_ready","step":"sample"}}), &c).unwrap().1, "Project: the sample is ready to check.");
    }
    #[test]
    fn real_pause_payload_uses_relative_resume_time() {
        let mut c = NotifyCtx::default();
        c.now = SystemTime::UNIX_EPOCH + Duration::from_secs(1795000000 - 3 * 3600 - 15 * 60);
        let n = notification_for(&json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"quota","resume_at":1795000000}}), &c).unwrap();
        assert_eq!(n.1, "Resumes on its own in about 3 h 15 min");
    }
    #[test]
    fn battery_requires_active_job() {
        assert!(battery_notification(&NotifyCtx::default(), Some(42)).is_none());
    }

    #[test]
    fn handler_path_dispatches_real_state_payload_and_deduplicates() {
        let mut ctx = NotifyCtx::default();
        ctx.project_titles
            .insert("project-1".into(), "Project".into());
        let event = json!({"method":"job/state","params":{"job":"job-1","project":"project-1","state":"sample_ready","step":"sample"}});
        let mut sent = Vec::new();
        for _ in 0..2 {
            dispatch_notification(&mut ctx, &event, SystemTime::UNIX_EPOCH, |title, body| {
                sent.push((title, body));
                Ok::<(), ()>(())
            })
            .unwrap();
        }
        assert_eq!(sent.len(), 1);
    }
}
