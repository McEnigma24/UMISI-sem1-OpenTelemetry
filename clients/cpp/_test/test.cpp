// clang-format off
#include <gtest/gtest.h> // must be first
// clang-format on
#include "__preprocessor__.h"

// 1) Prosty test niezależny
TEST(SampleTest, Sanity_2) { EXPECT_EQ(1, 1); }

// 2) TEST_F – testy oparte na fixtures
class MyFixture : public ::testing::Test
{
protected:
    std::vector<int> data;

    void SetUp() override
    {
        // kod wykonywany przed każdym testem
        data = {0, 1, 2, 3, 4};
    }

    void TearDown() override
    {
        // kod wykonywany po każdym teście
        data.clear();
    }
};

TEST_F(MyFixture, SumIsCorrect)
{
    int sum = 0;
    for (int v : data)
        sum += v;
    EXPECT_EQ(sum, 0 + 1 + 2 + 3 + 4);
}

TEST_F(MyFixture, FirstElementIsZero)
{
    ASSERT_FALSE(data.empty());
    EXPECT_EQ(data.front(), 0);
}

// 3) TEST_P + INSTANTIATE_TEST_CASE_P – testy parametryzowane
typedef std::tuple<int, bool> testParams;
class ParityTest : public ::testing::TestWithParam<testParams>
{
};

// clang-format off
INSTANTIATE_TEST_CASE_P(ParityChecks, ParityTest, 
::testing::Values(
                    testParams(0, true)
                    , testParams(1, false)
                    , testParams(42, true)
                    , testParams(17, false)
));
// clang-format on

TEST_P(ParityTest, CheckEven)
{
    auto [value, isEven] = GetParam(); // C++17 structured binding
    EXPECT_EQ((value % 2 == 0), isEven);
}

int main(int argc, char** argv)
{
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
