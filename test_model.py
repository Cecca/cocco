from sqlmodel import SQLModel, Session, create_engine, select
from cocco.model import Challenge, Course, QueuedSubmission, Submission, TestCase, User, UserRole, enqueue_submission

def test_queue():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    u1 = User(email="pippo@unipd.it", role=UserRole.student, nickname="pippo")
    u2 = User(email="pluto@unipd.it", role=UserRole.student, nickname="pluto")
    course = Course(title="Test", description="A fake course")
    session.add_all([u1, u2, course])
    session.commit()

    challenge = Challenge(
        title="First challenge",
        description="Ahahahah",
        course_id=course.id,
        max_memory_bytes=1024,
        max_time_s=60,
    )
    session.add(challenge)
    session.commit()

    test_case = TestCase(
        challenge_id=challenge.id,
        execution_order=0,
        input_path="/var/in",
        expected_output_path="/var/expected_output"
    )
    session.add(test_case)
    session.commit()

    submissions = [
        Submission(user_id=u1.id, challenge_id=challenge.id, language="c", source="asdfasdf"),
        Submission(user_id=u2.id, challenge_id=challenge.id, language="c", source="asdfasdf"),
    ]
    session.add_all(submissions)
    session.commit()

    for s in submissions:
        enqueue_submission(s, session)
    session.commit()
    for r in session.exec(select(QueuedSubmission)):
        print(r)


    


