SELECT i._id AS inspection_id, i.part, i.inspected_at, i.pixel_width,
       c.mm_per_pixel, p.min_mm, p.max_mm,
       ROUND(i.pixel_width * c.mm_per_pixel, 3) AS width_mm,
       CASE
         WHEN c._id IS NULL OR p._id IS NULL THEN 'unknown'
         WHEN ROUND(i.pixel_width * c.mm_per_pixel, 3)
              BETWEEN p.min_mm AND p.max_mm THEN 'pass'
         ELSE 'fail'
       END AS assessment
FROM inspection AS i
LEFT JOIN calibration FOR ALL VALID_TIME AS c
  ON c._id = i.camera_id
 AND c._valid_time CONTAINS i.inspected_at
LEFT JOIN product_spec FOR ALL VALID_TIME AS p
  ON p._id = i.product_id
 AND p._valid_time CONTAINS i.inspected_at
WHERE i.run_id = %s
ORDER BY i.inspected_at
