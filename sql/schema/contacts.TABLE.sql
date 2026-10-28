-- Name: contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contacts (
    phone text NOT NULL,
    name text NOT NULL,
    phone_display text,
    bit_names text[] DEFAULT '{}'::text[] NOT NULL,
    filter_checked_at timestamp with time zone,
    filter_tx_count integer,
    raw jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);


--
