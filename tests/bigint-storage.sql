BEGIN;
INSERT INTO validators(public_key, name, total_stake, token_decimals, status) VALUES ('bigint-boundary', 'test', 1361129467683753853853498429727072845824, 18, 'active');
DO $$ BEGIN
  IF (SELECT total_stake::text FROM validators WHERE public_key='bigint-boundary') <> '1361129467683753853853498429727072845824' THEN RAISE EXCEPTION 'precision lost'; END IF;
  IF (SELECT token_decimals FROM validators WHERE public_key='bigint-boundary') <> 18 THEN RAISE EXCEPTION 'wrong decimals'; END IF;
  UPDATE validators SET total_stake=115792089237316195423570985008687907853269984665640564039457584007913129639935 WHERE public_key='bigint-boundary';
  BEGIN
    UPDATE validators SET total_stake=115792089237316195423570985008687907853269984665640564039457584007913129639936 WHERE public_key='bigint-boundary';
    RAISE EXCEPTION 'accepted overflow';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    UPDATE validators SET total_stake=-1 WHERE public_key='bigint-boundary';
    RAISE EXCEPTION 'accepted negative';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END $$;

CREATE TEMP TABLE bigint_decimal_probe AS SELECT amount_dust, amount_asi, token_decimals FROM transfers WITH NO DATA;
INSERT INTO bigint_decimal_probe (amount_dust, amount_asi, token_decimals)
VALUES (1, 1e-77, 77),
       (115792089237316195423570985008687907853269984665640564039457584007913129639935,
        115792089237316195423570985008687907853269984665640564039457584007913129639935, 0);
DO $$ BEGIN
  IF (SELECT amount_asi FROM bigint_decimal_probe WHERE token_decimals=77) <> 1e-77 THEN
    RAISE EXCEPTION 'lost low raw unit at 77 decimals';
  END IF;
  IF (SELECT amount_asi FROM bigint_decimal_probe WHERE token_decimals=0) <>
     115792089237316195423570985008687907853269984665640564039457584007913129639935 THEN
    RAISE EXCEPTION 'lost maximum raw amount at zero decimals';
  END IF;
END $$;
ROLLBACK;
