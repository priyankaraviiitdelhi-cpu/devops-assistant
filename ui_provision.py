"""
ui_provision.py - the "Provision infrastructure" page.
Nothing is created in AWS until the user clicks "Approve and apply".
"""
import streamlit as st
import infra_agent as agent


def render():
    st.subheader("Provision infrastructure")
    st.write(
        "Describe what you want. The assistant writes Terraform, checks it against "
        "team policies and shows the plan. **Nothing is created until you click Approve.**"
    )

    request = st.text_area(
        "What should I create?",
        placeholder="Create a private S3 bucket for application logs with versioning enabled",
    )
    if st.button("Generate Terraform and plan", type="primary"):
        if not request.strip():
            st.warning("Please describe what you want to create.")
        else:
            with st.spinner("Writing Terraform and running plan (the first run downloads the AWS provider, about 1 minute)..."):
                try:
                    st.session_state.infra = agent.prepare(request)
                except Exception as e:
                    st.error(f"Something went wrong: {e}")

    if "infra" in st.session_state:
        _show_proposal(st.session_state.infra)

    st.divider()
    _show_stacks()


def _show_proposal(infra):
    if infra["code"]:
        st.markdown("#### Generated Terraform")
        st.code(infra["code"], language="hcl")

    if infra["issues"]:
        st.error("Blocked by guardrails:\n\n" + "\n".join(f"- {i}" for i in infra["issues"]))

    st.markdown(f"#### Plan: {infra['summary']}")
    if infra["plan"]:
        with st.expander("Full terraform output"):
            st.code(infra["plan"])

    if infra.get("applied"):
        st.success("Applied. You can see it under Deployed stacks below.")
        with st.expander("Apply output"):
            st.code(infra["apply_output"])
        return

    col1, col2 = st.columns(2)
    if col1.button("Approve and apply", type="primary", disabled=not infra["plan_ok"]):
        with st.spinner("Running terraform apply..."):
            ok, output = agent.apply(infra["workdir"])
        if ok:
            infra.update(applied=True, apply_output=output)
            st.rerun()
        else:
            st.error("terraform apply failed:")
            st.code(output)
    if col2.button("Reject"):
        agent.discard(infra["workdir"])
        del st.session_state.infra
        st.rerun()


def _show_stacks():
    st.markdown("#### Deployed stacks")
    stacks = agent.list_stacks()
    if not stacks:
        st.caption("Nothing deployed right now.")
        return
    for s in stacks:
        with st.container(border=True):
            st.markdown(f"**{s['name']}** - {s['request']}")
            st.caption("Resources: " + ", ".join(s["resources"]))
            if st.button("Destroy", key=f"destroy-{s['name']}"):
                with st.spinner("Running terraform destroy..."):
                    ok, output = agent.destroy(s["workdir"])
                if ok:
                    if st.session_state.get("infra", {}).get("workdir") == s["workdir"]:
                        del st.session_state.infra
                    st.rerun()
                else:
                    st.error("terraform destroy failed:")
                    st.code(output)