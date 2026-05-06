drop table if exists challenge;
create table challenge (
    id   INTEGER PRIMARY KEY,
    source  TEXT
);

insert into challenge(id, source) values
    (0, 'first challenge'),
    (1, 'second challenge')
;

drop table if exists test_case;
create table test_case(
    id  INTEGER PRIMARY KEY,
    challenge_id   INTEGER,
    input text
);
insert into test_case(id, challenge_id, input) values
    (0, 0, 'first test'),
    (1, 0, 'second test'),
    (2, 1, 'first test A'),
    (3, 1, 'second test A')
;


-- select row_to_json(test_case)
-- from test_case
-- where challenge_id = 0;

with
cases as (
    select * from test_case
),
cs as (select challenge.*, json_agg(cases) as cases
    from cases join challenge on cases.challenge_id = challenge.id
    group by challenge.id, challenge.source)
select json_agg(cs) from cs;

-- select json_object(
--     'id', id,
--     'cases', (select json_group_array(json_object('id', id, 'input', input))
--                 from test_case
--                 where challenge_id = 0)
-- ) as challenge
-- from challenge;
