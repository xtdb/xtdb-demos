SELECT p._id AS part_id, p.number, p.appearance, p.produced_at,
       m.defective AS predicted_defective, m.defect_score,
       m.model_version, m.line_action,
       r.defective AS reviewed_defective, r.source AS review_source,
       r.recorded_at AS reviewed_at
FROM part AS p
JOIN prediction AS m ON m._id = p._id
LEFT JOIN review AS r ON r._id = p._id
WHERE p.run_id = %s
ORDER BY p.number
