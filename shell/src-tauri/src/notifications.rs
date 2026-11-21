use serde_json::Value;
use std::{
    collections::HashMap,
    time::{Duration, SystemTime},
};

#[derive(Debug)]
pub struct NotifyCtx {
    pub project_titles: HashMap<String, String>,
    pub now: SystemTime,
    pub job_running: bool,
    pub previous_on_ac: Option<bool>,
    pub battery_percent: Option<u8>,
    seen: HashMap<(String, String), SystemTime>,
}

impl Default for NotifyCtx {
    fn default() -> Self {
        Self {
            project_titles: HashMap::new(),
            now: SystemTime::UNIX_EPOCH,
            job_running: false,
            previous_on_ac: None,
            battery_percent: None,
            seen: HashMap::new(),
        }
    }
}

pub fn notification_for(event: &Value, ctx: &NotifyCtx) -> Option<(String, String)> {
    let method = event.get("method").and_then(Value::as_str)?;
    let params = event.get("params").unwrap_or(event);
    match method {
        "job/state" => {
            let state = params
                .get("state")
                .or_else(|| params.get("status"))
                .and_then(Value::as_str)?;
            let project = params.get("project").and_then(Value::as_str).unwrap_or("");
            let title = ctx
                .project_titles
                .get(project)
                .map(String::as_str)
                .unwrap_or("A job");
            match state {
                "sample_ready" => Some((
                    "Sample ready to check".into(),
                    format!(
                        "{title}: {} leads read.",
                        params.get("total").and_then(Value::as_u64).unwrap_or(0)
                    ),
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
        "engine/event" if params.get("kind").and_then(Value::as_str) == Some("pause") => {
            let reason = params.get("reason").and_then(Value::as_str).unwrap_or("");
            if reason == "quota" {
                let time = params
                    .get("resumeAt")
                    .and_then(Value::as_str)
                    .unwrap_or("later");
                Some((
                    "Paused: usage limit".into(),
                    format!("Resumes on its own at {time}"),
                ))
            } else if ["credit", "credits", "spend", "spend_control"].contains(&reason) {
                Some((
                    "Paused to avoid paid credits".into(),
                    "Check that automatic top-up is off, then resume.".into(),
                ))
            } else {
                None
            }
        }
        "power/status"
            if ctx.job_running
                && ctx.previous_on_ac == Some(true)
                && params.get("onAc").and_then(Value::as_bool) == Some(false) =>
        {
            Some((
                "On battery".into(),
                format!(
                    "{}% left. Plug in to keep the job going.",
                    ctx.battery_percent
                        .or_else(|| params
                            .get("percent")
                            .and_then(Value::as_u64)
                            .map(|v| v.min(100) as u8))
                        .unwrap_or(0)
                ),
            ))
        }
        _ => None,
    }
}

pub fn should_notify(ctx: &mut NotifyCtx, event: &Value) -> bool {
    let method = event.get("method").and_then(Value::as_str).unwrap_or("");
    let params = event.get("params").unwrap_or(event);
    let key = (
        params
            .get("job")
            .or_else(|| params.get("project"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        params
            .get("state")
            .or_else(|| params.get("kind"))
            .and_then(Value::as_str)
            .unwrap_or(method)
            .to_owned(),
    );
    let now = ctx.now;
    if ctx
        .seen
        .get(&key)
        .is_some_and(|old| now.duration_since(*old).unwrap_or_default() < Duration::from_secs(600))
    {
        return false;
    }
    ctx.seen.insert(key, now);
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
    fn maps_job_notifications_without_private_data() {
        let mut c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        c.project_titles.insert("p".into(), "Project".into());
        let n = notification_for(
            &json!({"method":"job/state","params":{"project":"p","state":"sample_ready","total":12,"lead":"secret"}}),
            &c,
        );
        assert_eq!(
            n,
            Some((
                "Sample ready to check".into(),
                "Project: 12 leads read.".into()
            ))
        );
    }
    #[test]
    fn maps_pause_reasons() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert_eq!(notification_for(&json!({"method":"engine/event","params":{"kind":"pause","reason":"quota","resumeAt":"09:00"}}), &c).unwrap().0, "Paused: usage limit");
        assert_eq!(
            notification_for(
                &json!({"method":"engine/event","params":{"kind":"pause","reason":"credit"}}),
                &c
            )
            .unwrap()
            .0,
            "Paused to avoid paid credits"
        );
    }

    #[test]
    fn maps_done_and_done_with_issues() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert_eq!(
            notification_for(&json!({"method":"job/state","params":{"state":"done"}}), &c)
                .unwrap()
                .0,
            "Job finished"
        );
        assert_eq!(
            notification_for(
                &json!({"method":"job/state","params":{"state":"done_with_issues"}}),
                &c
            )
            .unwrap()
            .0,
            "Job finished with issues to check"
        );
    }

    #[test]
    fn maps_stalled() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert_eq!(
            notification_for(
                &json!({"method":"job/state","params":{"state":"stalled"}}),
                &c
            )
            .unwrap()
            .1,
            "No reply from the reader for a few minutes."
        );
    }

    #[test]
    fn unknown_project_uses_generic_title() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert_eq!(
            notification_for(&json!({"method":"job/state","params":{"state":"done"}}), &c)
                .unwrap()
                .1,
            "A job"
        );
    }

    #[test]
    fn non_notification_event_is_ignored() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert!(notification_for(
            &json!({"method":"chat/event","params":{"text":"private"}}),
            &c
        )
        .is_none());
    }
    #[test]
    fn notifications_are_deduplicated_per_job_and_kind_within_ten_minutes() {
        let mut c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        let e = json!({"method":"job/state","params":{"job":"j","state":"done"}});
        assert!(should_notify(&mut c, &e));
        assert!(!should_notify(&mut c, &e));
        assert!(should_notify(
            &mut c,
            &json!({"method":"job/state","params":{"job":"other-job","state":"done"}})
        ));
        assert!(should_notify(
            &mut c,
            &json!({"method":"job/state","params":{"job":"j","state":"stalled"}})
        ));
        c.now += Duration::from_secs(601);
        assert!(should_notify(&mut c, &e));
    }

    #[test]
    fn the_engine_handler_sends_a_notification_for_sample_ready_once() {
        let mut ctx = NotifyCtx::default();
        let event = json!({
            "method": "job/state",
            "params": {"job": "job-1", "project": "project-1", "state": "sample_ready", "total": 12}
        });
        let mut sent = Vec::new();
        for _ in 0..2 {
            dispatch_notification(&mut ctx, &event, SystemTime::UNIX_EPOCH, |title, body| {
                sent.push((title, body));
                Ok::<(), ()>(())
            })
            .unwrap();
        }
        assert_eq!(sent.len(), 1);
        assert_eq!(sent[0].0, "Sample ready to check");
    }
    #[test]
    fn battery_notification_requires_running_job_and_ac_transition() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            job_running: true,
            previous_on_ac: Some(true),
            battery_percent: Some(42),
            ..Default::default()
        };
        assert_eq!(
            notification_for(
                &json!({"method":"power/status","params":{"onAc":false}}),
                &c
            )
            .unwrap()
            .1,
            "42% left. Plug in to keep the job going."
        );
    }
}
