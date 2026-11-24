use serde_json::Value;
use std::collections::HashMap;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct JobView {
    pub state: Option<String>,
    pub pause_reason: Option<String>,
    pub done: u64,
    pub total: u64,
    pub eta: Option<String>,
    pub in_sample: bool,
    pub last_change: u64,
}

#[derive(Debug, Default)]
pub struct TrayModel {
    pub jobs: HashMap<String, JobView>,
    counter: u64,
}

fn params(event: &Value) -> &Value {
    event.get("params").unwrap_or(event)
}

fn running(state: Option<&str>) -> bool {
    matches!(
        state,
        Some("starting" | "running" | "active" | "in_progress")
    )
}

fn finished(state: Option<&str>) -> bool {
    matches!(
        state,
        Some("done" | "done_with_issues" | "failed" | "cancelled")
    )
}

impl TrayModel {
    pub fn apply(&mut self, notification: &Value) {
        let p = params(notification);
        let Some(job) = p.get("job").and_then(Value::as_str) else {
            return;
        };
        let method = notification.get("method").and_then(Value::as_str);
        let kind = p.get("kind").and_then(Value::as_str);
        if !matches!(method, Some("job/state" | "job/progress" | "engine/event")) {
            return;
        }
        if method == Some("engine/event") && !matches!(kind, Some("pause" | "eta")) {
            return;
        }
        self.counter += 1;
        let change = self.counter;
        let view = self.jobs.entry(job.to_owned()).or_default();
        view.last_change = change;
        match method {
            Some("job/state") => {
                view.state = p
                    .get("state")
                    .or_else(|| p.get("status"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                if running(view.state.as_deref()) {
                    view.pause_reason = None;
                }
                view.in_sample = view.state.as_deref() == Some("sample_ready");
            }
            Some("job/progress") => {
                view.done = p.get("done").and_then(Value::as_u64).unwrap_or(0);
                view.total = p.get("total").and_then(Value::as_u64).unwrap_or(0);
                if view.state.is_none() {
                    view.state = Some("running".into());
                }
            }
            Some("engine/event") if kind == Some("pause") => {
                view.state = Some("paused".into());
                view.pause_reason = p
                    .get("reason")
                    .and_then(Value::as_str)
                    .and_then(crate::notifications::pause_reason_label)
                    .map(str::to_owned);
            }
            Some("engine/event") if kind == Some("eta") => {
                view.eta = if p.get("known").and_then(Value::as_bool) == Some(false) {
                    None
                } else {
                    format_eta(
                        p.get("low").and_then(Value::as_f64),
                        p.get("high").and_then(Value::as_f64),
                    )
                };
            }
            _ => {}
        }
        if self
            .jobs
            .values()
            .filter(|job| finished(job.state.as_deref()))
            .count()
            > 20
        {
            if let Some(oldest) = self
                .jobs
                .iter()
                .filter(|(_, job)| finished(job.state.as_deref()))
                .min_by_key(|(_, job)| job.last_change)
                .map(|(id, _)| id.clone())
            {
                self.jobs.remove(&oldest);
            }
        }
    }

    pub fn engine_died(&mut self) {
        self.jobs.clear();
    }

    pub fn tooltip(&self) -> String {
        let chosen = self
            .jobs
            .values()
            .filter(|j| running(j.state.as_deref()))
            .max_by_key(|j| j.last_change)
            .map(|j| (j, 0))
            .or_else(|| {
                self.jobs
                    .values()
                    .find(|j| j.state.as_deref() == Some("stalled"))
                    .map(|j| (j, 1))
            })
            .or_else(|| {
                self.jobs
                    .values()
                    .find(|j| j.state.as_deref() == Some("paused"))
                    .map(|j| (j, 2))
            })
            .or_else(|| self.jobs.values().find(|j| j.in_sample).map(|j| (j, 3)))
            .or_else(|| {
                let newest = self.jobs.values().map(|j| j.last_change).max()?;
                self.jobs
                    .values()
                    .filter(|j| finished(j.state.as_deref()) && j.last_change == newest)
                    .max_by_key(|j| j.last_change)
                    .map(|j| (j, 4))
            });
        let Some((job, priority)) = chosen else {
            return "Bulk Call Analysis".into();
        };
        match priority {
            0 => format!(
                "Bulk Call Analysis — Reading {} of {}{}",
                format_number(job.done),
                format_number(job.total),
                job.eta
                    .as_deref()
                    .map(|e| format!(" · {e}"))
                    .unwrap_or_default()
            ),
            1 => "Stalled: no reply from the reader".into(),
            2 => job
                .pause_reason
                .as_deref()
                .map_or_else(|| "Paused".into(), |r| format!("Paused ({r})")),
            3 => "Sample ready to check".into(),
            _ => match job.state.as_deref() {
                Some("done") => "Done".into(),
                Some("done_with_issues") => "Done, with issues to check".into(),
                Some("failed") => "Stopped: the job failed".into(),
                _ => "Bulk Call Analysis".into(),
            },
        }
    }

    pub fn pausable_job(&self) -> Option<String> {
        self.jobs
            .iter()
            .filter(|(_, j)| running(j.state.as_deref()))
            .max_by_key(|(_, j)| j.last_change)
            .map(|(id, _)| id.clone())
    }

    pub fn holds_awake(&self) -> bool {
        self.jobs.values().any(|j| running(j.state.as_deref()))
    }
}

pub fn format_eta(low_seconds: Option<f64>, high_seconds: Option<f64>) -> Option<String> {
    let seconds = high_seconds.or(low_seconds)?;
    if !seconds.is_finite() || seconds <= 0.0 {
        return None;
    }
    Some(format!("{} left", format_duration(seconds.ceil() as u64)))
}

pub fn format_duration(seconds: u64) -> String {
    let minutes = seconds.div_ceil(60);
    if minutes < 60 {
        format!("about {minutes} min")
    } else {
        format!("about {} h", minutes / 60)
    }
}

pub fn pause_item_enabled(model: &TrayModel) -> bool {
    model.pausable_job().is_some()
}

fn format_number(value: u64) -> String {
    let digits = value.to_string();
    let first = if digits.len() % 3 == 0 {
        3
    } else {
        digits.len() % 3
    };
    let mut result = String::with_capacity(digits.len() + digits.len() / 3);
    result.push_str(&digits[..first]);
    for chunk in digits.as_bytes()[first..].chunks(3) {
        result.push(',');
        result.push_str(std::str::from_utf8(chunk).expect("numeric string is UTF-8"));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn model(events: &[Value]) -> TrayModel {
        let mut m = TrayModel::default();
        for event in events {
            m.apply(event);
        }
        m
    }
    fn state(job: &str, state: &str) -> Value {
        json!({"method":"job/state","params":{"job":job,"state":state}})
    }
    fn progress(job: &str) -> Value {
        json!({"method":"job/progress","params":{"job":job,"done":10,"total":20}})
    }
    fn pause(job: &str, reason: &str) -> Value {
        json!({"method":"engine/event","params":{"job":job,"kind":"pause","reason":reason}})
    }
    fn eta(job: &str) -> Value {
        json!({"method":"engine/event","params":{"job":job,"kind":"eta","low":60.0,"high":300.0}})
    }

    #[test]
    fn recovered_run_with_only_progress_reads_running() {
        assert!(model(&[progress("j")]).tooltip().contains("Reading"));
    }
    #[test]
    fn planning_a_second_job_never_hides_a_running_one() {
        let m = model(&[state("a", "running"), state("b", "planned"), progress("a")]);
        assert!(m.tooltip().contains("Reading"));
    }
    #[test]
    fn pause_and_eta_events_change_only_their_job() {
        let m = model(&[
            state("a", "running"),
            state("b", "running"),
            pause("b", "quota"),
            eta("b"),
        ]);
        assert_eq!(m.jobs["a"].state.as_deref(), Some("running"));
        assert_eq!(m.jobs["a"].eta, None);
    }
    #[test]
    fn progress_never_unpauses() {
        let m = model(&[state("j", "paused"), pause("j", "user"), progress("j")]);
        assert_eq!(m.jobs["j"].state.as_deref(), Some("paused"));
    }
    #[test]
    fn any_order_of_state_pause_and_progress_gives_the_same_tooltip() {
        let events = [state("j", "paused"), pause("j", "quota"), progress("j")];
        let expected = "Paused (allowance limit)";
        for order in [
            [0, 1, 2],
            [0, 2, 1],
            [1, 0, 2],
            [1, 2, 0],
            [2, 0, 1],
            [2, 1, 0],
        ] {
            assert_eq!(model(&order.map(|i| events[i].clone())).tooltip(), expected);
        }
    }
    #[test]
    fn holds_awake_only_while_running() {
        assert!(!model(&[state("j", "paused")]).holds_awake());
        assert!(model(&[state("j", "running")]).holds_awake());
    }
    #[test]
    fn pausable_job_follows_the_running_job() {
        let m = model(&[state("a", "running"), state("b", "running")]);
        assert_eq!(m.pausable_job().as_deref(), Some("b"));
    }
    #[test]
    fn engine_death_clears_everything() {
        let mut m = model(&[progress("j")]);
        m.engine_died();
        assert!(m.jobs.is_empty());
        assert_eq!(m.tooltip(), "Bulk Call Analysis");
    }

    #[test]
    fn pause_item_decision_is_disabled_without_a_running_job() {
        assert!(!pause_item_enabled(&model(&[])));
        assert!(pause_item_enabled(&model(&[state("j", "running")])));
        assert!(!pause_item_enabled(&model(&[state("j", "paused")])));
        assert!(!pause_item_enabled(&model(&[state("j", "done")])));
    }
}
