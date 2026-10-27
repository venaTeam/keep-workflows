import os

def process_file(filepath, replacements):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return
    with open(filepath, "r") as f:
        content = f.read()
    
    new_content = content
    for old, new in replacements:
        new_content = new_content.replace(old, new)
        
    with open(filepath, "w") as f:
        f.write(new_content)

# 1. maintenance_windows_bl.py
replacements_mw = [
    ("""            if alert.extra_data:
                payload.update(alert.extra_data)""", 
    """            enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
            if getattr(alert, "alert_enrichment", None):
                enrichments.update(alert.alert_enrichment.enrichments)
            payload.update(enrichments)"""),
            
    ("""                        f"from {alert.extra_data.get('previous_status') if alert.extra_data else 'unknown'} to {alert.status}\"""", 
     """                        f"from {(alert.alert_instance_enrichment.enrichments.get('previous_status') if getattr(alert, 'alert_instance_enrichment', None) else 'unknown')} to {alert.status}\""""),
     
    ("""            if not alert.extra_data or "previous_status" not in alert.extra_data:""",
     """            enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
            if "previous_status" not in enrichments:"""),
            
    ("""            if alert.extra_data:
                alert_payload.update(alert.extra_data)""",
     """            enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
            if getattr(alert, "alert_enrichment", None):
                enrichments.update(alert.alert_enrichment.enrichments)
            alert_payload.update(enrichments)""")
]
process_file("src/common/bl/maintenance_windows_bl.py", replacements_mw)

# 2. dismissal_expiry_bl.py
replacements_dismissal = [
    ("""                        if latest_alert.extra_data:
                            alert_data.update(latest_alert.extra_data)""",
     """                        enrichments = latest_alert.alert_instance_enrichment.enrichments if getattr(latest_alert, "alert_instance_enrichment", None) else {}
                        if getattr(latest_alert, "alert_enrichment", None):
                            enrichments.update(latest_alert.alert_enrichment.enrichments)
                        alert_data.update(enrichments)""")
]
process_file("src/common/bl/dismissal_expiry_bl.py", replacements_dismissal)

# 3. enrichments_bl.py
replacements_enrichments = [
    ("""        if alert.extra_data:
            alert_payload.update(alert.extra_data)""",
     """        enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
        if getattr(alert, "alert_enrichment", None):
            enrichments.update(alert.alert_enrichment.enrichments)
        alert_payload.update(enrichments)"""),
        
    ("""                        if latest_alert.extra_data:
                            alert_data.update(latest_alert.extra_data)""",
     """                        enrichments = latest_alert.alert_instance_enrichment.enrichments if getattr(latest_alert, "alert_instance_enrichment", None) else {}
                        if getattr(latest_alert, "alert_enrichment", None):
                            enrichments.update(latest_alert.alert_enrichment.enrichments)
                        alert_data.update(enrichments)""")
]
process_file("src/common/bl/enrichments_bl.py", replacements_enrichments)

# 4. db.py
replacements_db = [
    ("""    if not alert.extra_data:
        alert.extra_data = {}
    if mw_id in alert.extra_data.get("maintenance_windows_trace", []):
        return
    with existed_or_new_session(session) as session:
        if "maintenance_windows_trace" in alert.extra_data:
            if mw_id not in alert.extra_data["maintenance_windows_trace"]:
                alert.extra_data["maintenance_windows_trace"].append(mw_id)
        else:
            alert.extra_data["maintenance_windows_trace"] = [mw_id]
        flag_modified(alert, "extra_data")
        session.add(alert)
        session.commit()""",
    """    from src.common.core.db import enrich_entity
    from src.common.models.action_type import ActionType
    
    enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
    trace = enrichments.get("maintenance_windows_trace", [])
    
    if mw_id in trace:
        return
        
    with existed_or_new_session(session) as session:
        trace.append(mw_id)
        enrich_entity(
            alert.tenant_id,
            str(alert.id),
            {"maintenance_windows_trace": trace},
            action_callee="system",
            action_type=ActionType.GENERIC_ENRICH,
            action_description="Added maintenance window trace",
            session=session
        )"""),
        
    ("""    with existed_or_new_session(session) as session:
        try:
            status = alert.status
            prev_status = alert.extra_data.get("previous_status") if alert.extra_data else None
            alert.status = prev_status
            if not alert.extra_data:
                alert.extra_data = {}
            alert.extra_data["previous_status"] = status
            
            # Use raw query instead of ORM to avoid rewriting the whole object, as multiple alerts can be recovered simultaneously.
            query = update(Alert).where(Alert.id == alert.id).values(status=alert.status, extra_data=alert.extra_data)
            session.execute(query)
            session.commit()""",
    """    from src.common.core.db import enrich_entity
    from src.common.models.action_type import ActionType
    
    with existed_or_new_session(session) as session:
        try:
            status = alert.status
            enrichments = alert.alert_instance_enrichment.enrichments if getattr(alert, "alert_instance_enrichment", None) else {}
            prev_status = enrichments.get("previous_status")
            if not prev_status:
                logger.warning(f"Alert {alert.id} does not have previous status.")
                return
                
            alert.status = prev_status
            
            # Use raw query instead of ORM to avoid rewriting the whole object, as multiple alerts can be recovered simultaneously.
            query = update(Alert).where(Alert.id == alert.id).values(status=alert.status)
            session.execute(query)
            session.commit()
            
            enrich_entity(
                alert.tenant_id,
                str(alert.id),
                {"previous_status": status},
                action_callee="system",
                action_type=ActionType.GENERIC_ENRICH,
                action_description="Maintenance window status swap",
                session=session
            )""")
]
process_file("src/common/core/db.py", replacements_db)
