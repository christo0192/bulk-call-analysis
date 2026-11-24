use serde_json::Value;
use std::{
    collections::HashMap,
    time::{Duration, SystemTime},
};

#[derive(Debug)]
pub struct NotifyCtx {
    pub project_titles: HashMap<String, String>,
    pub previous_power: Option<crate::power::PowerStatus>,
    pub now: SystemTime,
    seen: HashMap<(String, String), SystemTime>,
}

impl Default for NotifyCtx {
    fn default() -> Self {
        Self {
            project_titles: HashMap::new(),
            previous_power: None,
            now: SystemTime::UNIX_EPOCH,
            seen: HashMap::new(),
        }
    }
}

fn params(event: &Value) -> &Value {
    event.get("params").unwrap_or(event)
}

pub fn pause_reason_label(reason: &str) -> Option<&'static str> {
    match reason {
        "quota" => Some("allowance limit"),
        "user" => Some("you"),
        "sleep" => Some("computer asleep"),
        "credit" => Some("paid credits"),
        "auth" => Some("sign-in needed"),
        "model_unavailable" => Some("model unavailable"),
        "error_storm" => Some("too many errors"),
        "network" => Some("network unavailable"),
        "spend_control" => Some("spending limit"),
        "power" => Some("power unavailable"),
        _ => None,
    }
}

fn relative_resume(now: SystemTime, value: Option<&Value>) -> String {
    let Some(seconds) = value.and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|n| n as i64)))
    else {
        return "Press Resume when your allowance is back.".into();
    };
    if !(946_684_800..=4_134_038_400).contains(&seconds) {
        return "Press Resume when your allowance is back.".into();
    }
    let Some(target) = SystemTime::UNIX_EPOCH.checked_add(Duration::from_secs(seconds as u64))
    else {
        return "Press Resume when your allowance is back.".into();
    };
    let seconds = target.duration_since(now).map(|d| d.as_secs()).unwrap_or(0);
    if seconds == 0 {
        return "Press Resume when your allowance is back.".into();
    }
    let time = if seconds < 60 {
        "less than a minute".into()
    } else {
        crate::tray_model::format_duration(seconds)
    };
    format!("Starts again on its own in {time}.")
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
            let raw_reason = p.get("reason").and_then(Value::as_str).unwrap_or("");
            pause_reason_label(raw_reason)?;
            let body = if raw_reason == "quota" {
                relative_resume(ctx.now, p.get("resume_at").or_else(|| p.get("resumeAt")))
            } else if raw_reason == "credit" {
                "Check that automatic top-up is off, then resume.".into()
            } else if raw_reason == "spend_control" {
                "Check your ChatGPT plan, then resume.".into()
            } else if raw_reason == "auth" {
                "Sign in to ChatGPT again, then resume.".into()
            } else {
                return None;
            };
            let title = match raw_reason {
                "quota" => "Paused: ChatGPT allowance",
                "credit" => "Paused to avoid paid credits",
                "spend_control" => "Paused: spending limit reached",
                "auth" => "Sign in needed",
                _ => unreachable!(),
            };
            Some((title.into(), body))
        }
        _ => None,
    }
}

pub fn battery_notification(held: bool, percent: Option<u8>) -> Option<(String, String)> {
    held.then_some(())?;
    Some((
        "On battery".into(),
        percent.map_or_else(
            || "On battery. Plug in to keep the job going.".into(),
            |p| format!("On battery: {p}% left. Plug in to keep the job going."),
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
    let key = if p.get("kind").and_then(Value::as_str) == Some("pause") {
        (
            key.0,
            format!(
                "pause:{}",
                p.get("reason").and_then(Value::as_str).unwrap_or("")
            ),
        )
    } else {
        key
    };
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
    fn project_title_lookup_is_coalesced_and_falls_back_to_a_job() {
        let c = NotifyCtx::default();
        let event =
            json!({"method":"job/state","params":{"job":"j","project":"unknown","state":"done"}});
        assert_eq!(notification_for(&event, &c).unwrap().1, "A job");
        assert_eq!(notification_for(&event, &c).unwrap().1, "A job");
    }
    #[test]
    fn real_pause_payload_uses_relative_resume_time() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH + Duration::from_secs(1795000000 - 3 * 3600 - 15 * 60),
            ..Default::default()
        };
        let n = notification_for(&json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"quota","resume_at":1795000000}}), &c).unwrap();
        assert_eq!(n.1, "Starts again on its own in about 3 h.");
    }
    #[test]
    fn battery_requires_active_job() {
        assert!(battery_notification(false, Some(42)).is_none());
    }

    #[test]
    fn pause_toasts_only_for_quota_credit_spend_control_and_auth() {
        let reasons = [
            "quota",
            "user",
            "sleep",
            "credit",
            "auth",
            "model_unavailable",
            "error_storm",
            "network",
            "spend_control",
            "power",
        ];
        let mut c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000),
            ..Default::default()
        };
        for reason in reasons {
            let event = json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":reason,"resume_at":1_800_010_000}});
            let result = notification_for(&event, &c);
            assert_eq!(
                result.is_some(),
                matches!(reason, "quota" | "credit" | "auth" | "spend_control"),
                "{reason}"
            );
            c.now += Duration::from_secs(1);
        }
    }

    #[test]
    fn toast_texts_for_credit_spend_control_and_auth() {
        let cases = [
            (
                "credit",
                "Paused to avoid paid credits",
                "Check that automatic top-up is off, then resume.",
            ),
            (
                "spend_control",
                "Paused: spending limit reached",
                "Check your ChatGPT plan, then resume.",
            ),
            (
                "auth",
                "Sign in needed",
                "Sign in to ChatGPT again, then resume.",
            ),
        ];
        for (reason, title, body) in cases {
            let event = json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":reason}});
            assert_eq!(
                notification_for(&event, &NotifyCtx::default()),
                Some((title.into(), body.into()))
            );
        }
    }

    #[test]
    fn battery_toast_repeats_after_ten_minutes() {
        let event = json!({"method":"engine/event","params":{"job":"j","kind":"battery"}});
        let mut ctx = NotifyCtx {
            now: SystemTime::UNIX_EPOCH,
            ..Default::default()
        };
        assert!(should_notify(&mut ctx, &event));
        ctx.now += Duration::from_secs(601);
        assert!(should_notify(&mut ctx, &event));
    }

    #[test]
    fn user_pause_does_not_suppress_a_quota_toast() {
        let mut c = NotifyCtx::default();
        let user =
            json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"user"}});
        let quota =
            json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"quota"}});
        assert!(
            !dispatch_notification(
                &mut c,
                &user,
                SystemTime::UNIX_EPOCH,
                |_, _| Ok::<_, ()>(())
            )
            .unwrap()
        );
        assert!(
            dispatch_notification(&mut c, &quota, SystemTime::UNIX_EPOCH, |_, _| Ok::<_, ()>(
                ()
            ))
            .unwrap()
        );
    }

    #[test]
    fn out_of_range_resume_at_does_not_panic() {
        let c = NotifyCtx {
            now: SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000),
            ..Default::default()
        };
        let e = json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"quota","resume_at":i64::MAX}});
        assert_eq!(
            notification_for(&e, &c).unwrap().1,
            "Press Resume when your allowance is back."
        );
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

    #[test]
    fn maps_done_and_done_with_issues() {
        let c = NotifyCtx::default();
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
        assert_eq!(
            notification_for(
                &json!({"method":"job/state","params":{"state":"stalled"}}),
                &NotifyCtx::default()
            )
            .unwrap()
            .1,
            "No reply from the reader for a few minutes."
        );
    }

    #[test]
    fn non_notification_event_is_ignored() {
        assert!(notification_for(
            &json!({"method":"chat/event","params":{"text":"synthetic"}}),
            &NotifyCtx::default()
        )
        .is_none());
    }

    #[test]
    fn notifications_are_deduplicated_per_job_and_kind_within_ten_minutes() {
        let mut c = NotifyCtx::default();
        let event = json!({"method":"job/state","params":{"job":"j","state":"done"}});
        assert!(should_notify(&mut c, &event));
        assert!(!should_notify(&mut c, &event));
        assert!(should_notify(
            &mut c,
            &json!({"method":"job/state","params":{"job":"other","state":"done"}})
        ));
        assert!(should_notify(
            &mut c,
            &json!({"method":"job/state","params":{"job":"j","state":"stalled"}})
        ));
        c.now += Duration::from_secs(601);
        assert!(should_notify(&mut c, &event));
    }

    #[test]
    fn quota_pause_without_a_reset_time_never_promises_a_resume() {
        let event = json!({"method":"engine/event","params":{"kind":"pause","job":"j","reason":"quota","resume_at":null}});
        assert_eq!(
            notification_for(&event, &NotifyCtx::default()).unwrap().1,
            "Press Resume when your allowance is back."
        );
    }

    #[test]
    fn battery_transition_toasts_once_and_hides_unknown_percent() {
        let ac = crate::power::PowerStatus {
            on_ac: Some(true),
            percent: Some(80),
        };
        let battery = crate::power::PowerStatus {
            on_ac: Some(false),
            percent: None,
        };
        assert!(crate::engine::battery_transition(Some(ac), battery, true));
        assert!(!crate::engine::battery_transition(
            Some(battery),
            battery,
            true
        ));
        assert_eq!(
            battery_notification(true, None).unwrap().1,
            "On battery. Plug in to keep the job going."
        );
    }
}
