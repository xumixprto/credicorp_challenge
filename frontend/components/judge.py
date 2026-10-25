import pandas as pd
import streamlit as st

from frontend.services.api_client import APIError
from frontend.services.judge_client import JudgeClient


def render_judge(client: JudgeClient) -> None:
    st.title("LLM Judge")
    st.caption("Review retrieval quality, answer evidence, tool usage, and intention success.")
    try:
        table = client.table()
    except APIError as exc:
        st.error(str(exc))
        st.button("Refresh", key="judge_refresh_error")
        return
    total, missing, shown = st.columns(3)
    total.metric("Recorded runs", table.total)
    missing.metric("Missing evaluations", table.missing)
    shown.metric("Rows shown", len(table.rows))
    download_evaluations, download_runs = st.columns(2)
    with download_evaluations:
        st.download_button(
            "Download evaluations CSV",
            data=lambda: client.download_csv("evaluations"),
            file_name="evaluations.csv",
            mime="text/csv",
            icon=":material/download:",
            key="judge_download_evaluations",
            on_click="ignore",
            disabled=table.total == 0,
        )
    with download_runs:
        st.download_button(
            "Download runs CSV",
            data=lambda: client.download_csv("runs"),
            file_name="runs.csv",
            mime="text/csv",
            icon=":material/download:",
            key="judge_download_runs",
            on_click="ignore",
            disabled=table.total == 0,
        )
    st.caption(
        "Downloads contain the complete CSV tables, with the latest evaluation per run and "
        "rows beyond the 50 shown below. Join the files using run_id."
    )
    st.caption(
        "Showing the 50 most recent runs, including unevaluated runs. "
        "Recall needs reference labels. Blank scores mean not applicable or unavailable. "
        "Without labels, NDCG measures ordering within retrieved results. "
        "Hallucination is the fraction of unsupported or contradicted factual claims; "
        "lower is better. Tool usage and intention success: 0 = failed, 0.5 = partial, "
        "1 = successful; higher is better. Correctly using no tool can score 1."
    )
    last = st.session_state.get("judge_result")
    if last:
        failures = sum(item.status == "error" for item in last.evaluations)
        st.info(
            f"Last evaluation: {last.selected} runs processed, {failures} failed, "
            f"{last.skipped} skipped. Completed results are saved."
        )
    if not table.rows:
        st.info("No runs recorded yet. Send a message in Agent Chat to create the first run.")
        return
    records = []
    scores = [
        "precision_at_k",
        "recall_at_k",
        "ndcg_at_k",
        "hallucination",
        "groundedness",
        "tool_usage",
        "intention_success",
    ]
    score_labels = {
        "precision_at_k": "Precision@k",
        "recall_at_k": "Recall@k",
        "ndcg_at_k": "NDCG@k",
        "hallucination": "Hallucination",
        "groundedness": "Groundedness",
        "tool_usage": "Tool usage",
        "intention_success": "Intention success",
    }
    for row in table.rows:
        evaluation = row.evaluation
        records.append(
            {
                "Select": False,
                "run_id": row.run_id,
                "query": row.query,
                "run_status": row.run_status,
                "evaluation_status": row.evaluation_status,
                "k": evaluation.k if evaluation else None,
                **{name: getattr(evaluation, name) if evaluation else None for name in scores},
                "answer": row.answer,
                "session_id": row.session_id,
                "user_id": row.user_id,
                "started_at": row.started_at.isoformat(),
            }
        )
    frame = pd.DataFrame(records)
    version = st.session_state.get("judge_table_version", 0)
    edited = st.data_editor(
        frame,
        key=f"judge_selection_{version}",
        hide_index=True,
        width="stretch",
        disabled=[column for column in frame.columns if column != "Select"],
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", default=False),
            "query": st.column_config.TextColumn("Question", width="large"),
            **{
                name: st.column_config.NumberColumn(score_labels[name], format="%.3f")
                for name in scores
            },
        },
    )
    selected = edited.loc[edited["Select"], "run_id"].tolist()
    evaluate, evaluate_missing, refresh = st.columns([1, 1, 1])
    action = None
    with evaluate:
        if st.button(
            "Evaluate selected",
            key="judge_evaluate_selected",
            type="primary",
            disabled=not selected,
            width="stretch",
        ):
            action = "selected"
    with evaluate_missing:
        if st.button(
            "Evaluate Missing Runs",
            key="judge_evaluate_missing",
            disabled=table.missing == 0,
            width="stretch",
        ):
            action = "missing"
    with refresh:
        if st.button("Refresh table", key="judge_refresh", width="stretch"):
            st.session_state.judge_table_version = version + 1
            st.rerun()
    st.caption(
        "Evaluate Missing Runs covers the whole run table, including runs beyond these 50 rows. "
        "Selected runs are evaluated again even when they already have scores."
    )
    if action:
        with st.spinner("Evaluating runs in batches. Results are saved after each batch..."):
            try:
                st.session_state.judge_result = client.evaluate(
                    selected if action == "selected" else None
                )
                st.session_state.judge_table_version = version + 1
                st.rerun()
            except APIError as exc:
                st.error(str(exc))
    with st.expander("Inspect an evaluation"):
        run_id = st.selectbox("Run ID", [row.run_id for row in table.rows], key="judge_detail_run")
        row = next(row for row in table.rows if row.run_id == run_id)
        if row.evaluation:
            st.write(row.evaluation.summary)
            for name in ("tool_usage", "intention_success"):
                assessment = row.evaluation.assessment_json.get(name)
                if assessment:
                    st.markdown(f"**{score_labels[name]}:** {assessment['reason']}")
            st.json(row.evaluation.model_dump(mode="json"), expanded=False)
        else:
            st.caption("This run has not been evaluated yet.")
