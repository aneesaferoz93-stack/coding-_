-- ============================================================================
-- 003_pilot_seed.sql — Phase 0 pilot: Peshawar, Mardan, Swat.
--
-- Only these three districts. Not all fourteen. That is the entire point of
-- Phase 0, and it is a change of behaviour from how Punjab rolled out: Punjab
-- began the Green Property Certificate in Sahiwal on 1 May 2026, extended to
-- Lodhran and Hafizabad on 1 July, made it mandatory province-wide on 1 July,
-- and by 11 July had to restore Fard issuance in non-digitised areas because
-- the alternative could not serve every case. See docs/03-punjab-postmortem.md.
--
-- NOTE ON THE NUMBERS
--   Mouza outlines below are rectangular bounding boxes, not real cadastral
--   polygons, and the DC rates are illustrative. They exist so the system runs
--   end to end on day one. Replace them with the output of the Settlement &
--   Digitization of Land Records (SDLR) programme and the notified rate
--   schedule before any real pilot.
-- ============================================================================

BEGIN;

INSERT INTO district (code, name_en, name_ur, name_ps, phase, live, e_registry_live) VALUES
    ('PESHAWAR', 'Peshawar', 'پشاور', 'پېښور', 'PHASE_0_PILOT', TRUE, TRUE),
    ('MARDAN',   'Mardan',   'مردان', 'مردان', 'PHASE_0_PILOT', TRUE, TRUE),
    ('SWAT',     'Swat',     'سوات',  'سوات',  'PHASE_0_PILOT', TRUE, TRUE)
ON CONFLICT (code) DO NOTHING;

INSERT INTO mouza (code, district_code, tehsil_code, name_en, name_ur, name_ps,
                   boundary_wkt, centroid_lat, centroid_lon, digitised,
                   latha_scanned_pct, total_khasras) VALUES
    ('MZ-PSH-001', 'PESHAWAR', 'PESHAWAR',     'Landi Arbab',  'لنڈی ارباب', 'لنډي ارباب',
     'POLYGON((71.6000 34.0200, 71.6500 34.0200, 71.6500 34.0700, 71.6000 34.0700, 71.6000 34.0200))',
     34.045, 71.625, TRUE, 88.0, 412),
    ('MZ-PSH-002', 'PESHAWAR', 'PESHAWAR',     'Mattani',      'متنی',       'متني',
     'POLYGON((71.5600 33.9300, 71.6100 33.9300, 71.6100 33.9800, 71.5600 33.9800, 71.5600 33.9300))',
     33.955, 71.585, TRUE, 74.0, 355),
    ('MZ-MRD-002', 'MARDAN',   'MARDAN',       'Bakhshali',    'بخشالی',     'بخشالي',
     'POLYGON((71.9900 34.2600, 72.0500 34.2600, 72.0500 34.3100, 71.9900 34.3100, 71.9900 34.2600))',
     34.285, 72.020, TRUE, 81.5, 508),
    ('MZ-MRD-003', 'MARDAN',   'TAKHT-I-BHAI', 'Takht-i-Bhai', 'تخت بھائی',  'تخت بهايي',
     'POLYGON((71.9100 34.2800, 71.9800 34.2800, 71.9800 34.3400, 71.9100 34.3400, 71.9100 34.2800))',
     34.310, 71.945, TRUE, 69.0, 631),
    ('MZ-SWT-001', 'SWAT',     'BABUZAI',      'Saidu Sharif', 'سیدو شریف',  'سيدو شريف',
     'POLYGON((72.3900 34.9600, 72.4600 34.9600, 72.4600 35.0200, 72.3900 35.0200, 72.3900 34.9600))',
     34.990, 72.425, TRUE, 92.0, 447),
    ('MZ-SWT-002', 'SWAT',     'MATTA',        'Matta',        'مٹہ',        'مټه',
     'POLYGON((72.5800 35.0200, 72.6600 35.0200, 72.6600 35.0900, 72.5800 35.0900, 72.5800 35.0200))',
     35.055, 72.620, TRUE, 63.0, 589)
ON CONFLICT (code) DO NOTHING;

-- Deputy Commissioner valuation table. Rule 2 is meaningless without it, and
-- the rate must be the *notified* rate, loaded with its notification number.
INSERT INTO dc_rate (district_code, mouza_code, land_type, rate_per_kanal,
                     effective_from, source_notification) VALUES
    ('PESHAWAR', NULL,         'AGRICULTURAL', 1200000, '2026-07-01', 'Illustrative FY2026-27'),
    ('PESHAWAR', 'MZ-PSH-001', 'AGRICULTURAL', 1200000, '2026-07-01', 'Illustrative FY2026-27'),
    ('PESHAWAR', 'MZ-PSH-002', 'AGRICULTURAL', 1500000, '2026-07-01', 'Illustrative FY2026-27'),
    ('MARDAN',   NULL,         'AGRICULTURAL',  700000, '2026-07-01', 'Illustrative FY2026-27'),
    ('MARDAN',   'MZ-MRD-002', 'AGRICULTURAL',  700000, '2026-07-01', 'Illustrative FY2026-27'),
    ('MARDAN',   'MZ-MRD-003', 'AGRICULTURAL',  850000, '2026-07-01', 'Illustrative FY2026-27'),
    ('SWAT',     NULL,         'AGRICULTURAL',  900000, '2026-07-01', 'Illustrative FY2026-27'),
    ('SWAT',     'MZ-SWT-001', 'AGRICULTURAL',  900000, '2026-07-01', 'Illustrative FY2026-27'),
    ('SWAT',     'MZ-SWT-002', 'AGRICULTURAL',  650000, '2026-07-01', 'Illustrative FY2026-27');

-- Scanned pre-computerisation register (the "latha").
-- Khasra 77 in Takht-i-Bhai deliberately still stands in the name of the
-- deceased father: that is the normal state of a KP inheritance record, and
-- Rule 5 has to handle it as "reconcile this", not as "you are a fraudster".
INSERT INTO latha_record (district_code, mouza_code, khasra_no, khewat_no,
                          owner_name, father_name, qaum, mouza_name_in_record,
                          area_kanal, jamabandi_year, scanned_page) VALUES
    ('PESHAWAR', 'MZ-PSH-001', '441',  '12', 'Gul Muhammad Khan', 'Sher Dil Khan', 'Khattak',  'Landi Arbab',   8.0,  2016, 'PSH-001/p.112'),
    ('PESHAWAR', 'MZ-PSH-002', '330',  '91', 'Nasreen Bibi',      'Ghulam Nabi',   'Mohmand',  'Mattani',      12.0,  2014, 'PSH-002/p.207'),
    ('MARDAN',   'MZ-MRD-002', '1902', '44', 'Zarin Gul',         'Amir Gul',      'Yousafzai','Bakhshali',    10.0,  2018, 'MRD-002/p.031'),
    ('MARDAN',   'MZ-MRD-003', '77',   '23', 'Rozi Khan',         'Sherin Khan',   'Yousafzai','Takht-i-Bhai',  5.0,  2012, 'MRD-003/p.149'),
    ('SWAT',     'MZ-SWT-001', '88',   '7',  'Mir Afzal Khan',    'Bahram Khan',   'Yousafzai','Saidu Sharif',  6.0,  2015, 'SWT-001/p.058'),
    ('SWAT',     'MZ-SWT-002', '1401', '55', 'Sher Zaman',        'Zaman Ali',     'Yousafzai','Matta',        20.0,  2017, 'SWT-002/p.402');

INSERT INTO officer (id, name, role, district_code, tehsil_code) VALUES
    ('PAT-PSH-07', 'Aurangzeb Khan',     'PATWARI',   'PESHAWAR', 'PESHAWAR'),
    ('PAT-PSH-12', 'Fida Muhammad',      'PATWARI',   'PESHAWAR', 'PESHAWAR'),
    ('PAT-MRD-03', 'Ihsanullah',         'PATWARI',   'MARDAN',   'MARDAN'),
    ('PAT-MRD-09', 'Nawab Ali',          'PATWARI',   'MARDAN',   'TAKHT-I-BHAI'),
    ('PAT-SWT-04', 'Zubair Ahmad',       'PATWARI',   'SWAT',     'MATTA'),
    ('PAT-SWT-11', 'Bakhtiar Ali',       'PATWARI',   'SWAT',     'BABUZAI'),
    ('TEH-PSH-01', 'Tehsildar Peshawar', 'TEHSILDAR', 'PESHAWAR', 'PESHAWAR')
ON CONFLICT (id) DO NOTHING;

COMMIT;
